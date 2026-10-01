---
title: 运维与排错
nav_order: 6
---

# 本地运行与运维手册
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本手册面向单机、本地磁盘和 SQLite 的运行方式。项目不依赖 PostgreSQL、对象存储、
Docker 或远程调度服务；它提供的是可追溯、可验证、可重跑的本地处理闭环。

## 1. 运行前检查

在项目根目录创建虚拟环境并安装运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

macOS 或 Linux 使用：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

开发检查额外安装 `.[dev]`：

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m unittest discover -s tests -v
```

设置 `.env` 时最重要的约束是：`FIN_DOC_DATA_ROOT` 必须与
`config/collection.json` 顶层的 `data_root` 指向同一个目录。默认两者都是 `data`。
OCR 使用自建 Serving 时设置 `PADDLEOCR_LOCAL_API_URL`；使用云端时仅设置
`PADDLEOCR_CLOUD_TOKEN`，不要将 Token 写入仓库或命令历史。

确认本地入口可用：

```powershell
python -m workflow.cli --data-root data list
python -m workflow.cli --data-root data overview
```

### 1.1 日志与排错

命令会同时向终端的 `stderr` 和 `logs/fin-doc-governance.log` 写入结构化运行事件，
而命令结果 JSON 仍写入 `stdout`。因此可以在查看实时日志时继续将结果交给脚本处理：

```powershell
# 正常执行时显示阶段、步骤、批次与运行 ID
python -m workflow.cli --log-level INFO run --pipeline collected_financial `
  --batch-id fin-2026-08-11 --ocr-backend local

# 排错时额外显示命令参数和发布文件逐项完整性校验事件
python -m workflow.cli --log-level DEBUG --log-color always verify-release `
  --batch-id fin-2026-08-11 --release-id <release_id>

# 供日志采集工具读取的逐行 JSON
python -m workflow.cli --log-format json overview
```

默认日志文件为 5 MiB，自动轮转并保留 5 份历史文件（`.1` 至 `.5`）。日志目录已被 Git 忽略，
不应提交运行记录。可以在 `.env` 设置 `FIN_DOC_LOG_FILE`、`FIN_DOC_LOG_MAX_BYTES`、
`FIN_DOC_LOG_BACKUP_COUNT`，或用同名命令行参数覆盖。`FIN_DOC_LOG_LEVEL` 支持 `DEBUG`、
`INFO`、`WARNING` 和 `ERROR`；`FIN_DOC_LOG_FORMAT=json` 适用于外部日志收集，
`FIN_DOC_LOG_COLOR=never` 适用于无交互终端。

## 2. 三条交付路径

| 输入状态 | 命令 | 使用场景 |
| --- | --- | --- |
| 手工提供 PDF | `local_financial` | 有可信的本地财报文件，需要完整处理 |
| 本轮采集的财报 | `collected_financial` | 复用采集器已有的原始 PDF 与来源信息 |
| 已有 OCR 输出 | `govern_and_publish` | OCR 已完成，只需要治理和发布 |

### 2.1 从本地 PDF 完整处理

```powershell
python -m workflow.cli --data-root data run --pipeline local_financial `
  --input .\samples\annual-report.pdf `
  --batch-id fin-2026-08-11 `
  --ocr-backend local `
  --stock-code 600519 --report-year 2023 --report-type annual `
  --company-name "贵州茅台" `
  --announcement-date 2024-04-03 `
  --title "贵州茅台2023年年度报告" `
  --llm-backend mock
```

执行成功后，输出中的 `workflow_run_id` 同时是该次自动发布的 `release_id`。
原始文件、OCR 结果和旧版本不会被覆盖；使用 `--force` 才会重建已有的阶段产物。

### 2.2 采集后接管财报

采集器在 `reports.jsonl` 中记录成功文件和 `batch_id` 的关联。接管阶段只接受在同一
数据根目录中存在、路径未越界且哈希一致的 PDF。

```powershell
python -m spiders.collector --config config/collection.json --data-root data `
  --batch-id fin-2026-08-11

python -m workflow.cli --data-root data run --pipeline collected_financial `
  --batch-id fin-2026-08-11 `
  --ocr-backend local `
  --llm-backend mock
```

没有成功采集报告时，工作流返回 `skipped` 并以退出码 `0` 结束。这表示本轮没有可处理
输入，不是 OCR 或治理失败。

### 2.3 治理已有 OCR 输出

```powershell
python -m workflow.cli --data-root data run --pipeline govern_and_publish `
  --batch-id fin-2026-08-11 --llm-backend mock
```

该路径要求对应 OCR 输出和来源清单已经存在。它不会重新下载、导入或 OCR。

## 3. 日常观察与交付校验

```powershell
# 近期批次、失败/运行中工作流、待复核数量和最新发布指针
python -m workflow.cli --data-root data overview

# 仅查看失败的工作流
python -m workflow.cli --data-root data runs --status failed

# 查看某次工作流的逐阶段输出和错误信息
python -m workflow.cli --data-root data status <workflow_run_id>

# 发布前检查，或重新计算已发布包的所有 SHA-256
python -m workflow.cli --data-root data validate --batch-id fin-2026-08-11
python -m workflow.cli --data-root data verify-release `
  --batch-id fin-2026-08-11 --release-id <release_id>

# 创建一致性 SQLite 快照；不会覆盖已有文件
python -m workflow.cli --data-root data backup-state
python -m workflow.cli --data-root data backup-state `
  --output .\data\backups\before-review.sqlite3
```

`verify-release` 会检查 `checksums.sha256` 的格式、重复文件名、缺失文件和哈希不匹配。
失败时返回非零退出码。备份使用 SQLite 在线备份并执行完整性检查；它是可独立打开的
快照，但项目没有提供自动覆盖恢复命令，恢复前应先人工验证目标数据库和业务影响。

### 3.1 构建继续预训练语料

训练语料只能消费已校验的发布包。先对每个发布包运行 `verify-release`，再使用新的
数据集 ID 构建；重复 `--release` 可合并多个包。构建过程不改变发布包、SQLite 状态或既有
工作流产物。

```powershell
python -m workflow.cli --data-root data verify-release `
  --batch-id fin-2026-08-11 --release-id <release_id>

python -m training.cli --data-root data --log-level INFO build-pretrain `
  --dataset-id financial-governed-v1 `
  --release fin-2026-08-11/<release_id> `
  --release research-2026-08-11/<release_id>
```

数据集写入 `data/training/pretrain/<dataset_id>/`，并且同名目录不能覆盖。`corpus.jsonl` 只含
脱敏后的治理 Markdown；`provenance.jsonl` 保存来源、身份、规则版本和 OCR 质量，
`excluded.jsonl` 记录空正文或去重排除，`manifest.json` 记录策略与汇总，
`checksums.sha256` 覆盖这四个数据文件。输出固定标记为 `internal-only`，并记录
`rights_status=unknown`，因此在实际训练前仍需进行授权与用途复核。

发布包为 v6 时使用 `governed-documents.jsonl` 中的完整正文。已校验 v5 包也可构建，但会从
`chunks.jsonl` 重组，并在 provenance 和 manifest 中标为 `lossy_chunk_reconstruction`。OCR
`needs_review` 的文档会保留在语料中，质量告警不会进入训练正文。

构建后可在 PowerShell 重新计算并比对训练数据集校验清单：

```powershell
$dataset = ".\data\training\pretrain\financial-governed-v1"
Get-Content "$dataset\checksums.sha256" | ForEach-Object {
  $expected, $name = $_ -split '\s{2}', 2
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $dataset $name)).Hash.ToLower()
  [PSCustomObject]@{ File = $name; Expected = $expected; Actual = $actual; Match = ($expected -eq $actual) }
}
```

## 4. 人工复核与恢复

工作流失败后先查看状态，再从已经成功的阶段之后继续：

```powershell
python -m workflow.cli --data-root data status <workflow_run_id>
python -m workflow.cli --data-root data resume <workflow_run_id>
```

OCR 和元数据质量告警不阻断发布，但应通过以下命令形成可审计的处置记录：

```powershell
# 查看待复核 OCR 与元数据
python -m workflow.cli --data-root data ocr-review --batch-id fin-2026-08-11
python -m workflow.cli --data-root data metadata-review --batch-id fin-2026-08-11

# 经人工确认后重跑一个 OCR 资产；旧输出会归档
python -m workflow.cli --data-root data ocr-retry `
  --batch-id fin-2026-08-11 --asset-id <asset_uid> `
  --ocr-backend local --reason "人工确认页面缺失"

# 写入带理由的元数据修正
python -m workflow.cli --data-root data metadata-override `
  --batch-id fin-2026-08-11 --asset-id <asset_uid> `
  --set company_name="贵州茅台" `
  --reason "根据已签发年报封面复核"
```

同一逻辑报告出现多个 PDF 版本时，先查看候选，再选择有效版本。候选文件不会删除：

```powershell
python -m workflow.cli --data-root data version-review --batch-id fin-2026-08-11
python -m workflow.cli --data-root data version-select `
  --batch-id fin-2026-08-11 `
  --report-group-id <report_group_uid> `
  --asset-id <asset_uid> `
  --reason "人工对比后选择修订完整版本"
```

若接管阶段报告哈希不匹配、路径不存在或路径越界，应停止该批次处理，检查
`data/manifests/reports.jsonl`、对应原始 PDF 和采集运行摘要。不要手工修改已发布的
`checksums.sha256` 或覆盖已有发布目录；修正输入后以相同 `batch_id` 新建一次工作流。

## 5. 定时运行

`scripts/run_collection.sh` 现在是每日数据飞轮的兼容入口，由 Python 安全加载 `.env`，串接新文档 OCR、图表治理和预训练数据生成。正式飞轮使用 `FIN_DOC_FLYWHEEL_OCR_BACKEND`，需要视觉模型、独立审核模型、目标 tokenizer 和来源训练准入。配置、预检、cron/launchd 模板与补跑方法见 [数据飞轮运行说明](data-flywheel.md)。

先完成少量前台实跑再启用系统调度；旧的独立流水线 CLI 可继续使用，但不要并发运行两套定时流程。

## 6. 本地目录与保留策略

| 路径 | 内容 | 处理原则 |
| --- | --- | --- |
| `data/raw_pdfs/` | 已校验的原始 PDF | 按哈希去重，不手工覆盖 |
| `data/quarantine/` | 无效或可疑下载文件 | 用于排查，不进入交付 |
| `data/manifests/` | 当前资产、元数据和人工复核投影 | 由程序原子写入 |
| `data/parsed_md/` | OCR Markdown、JSON 与图片 | OCR 重试时保留历史输出 |
| `data/processed/` | 治理 Markdown、JSON 和 chunks | 规则版本升级后保留旧产物 |
| `data/published/` | 不可变发布包与 `latest.json` | 只新增，不修改已发布版本 |
| `data/training/pretrain/` | 已校验发布包派生的继续预训练语料 | 只新增，保留 corpus、血缘、排除记录和校验清单 |
| `data/state/pipeline.db` | 运行、步骤、QC 与血缘状态 | 通过 `backup-state` 定期备份 |

单机模式适合一项采集/处理任务与若干只读观察命令并行运行。若未来需要多节点并发、
集中权限、对象存储、远程备份或告警，应在保留现有身份和交付契约的前提下替换存储、
调度和监控层，而不是绕过本地清单和校验机制。
