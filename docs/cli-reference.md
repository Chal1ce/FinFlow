---
title: 命令与参数
nav_order: 1
parent: 参考与设计
---

# 命令与参数参考
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本页集中列出采集、OCR、独立治理和传统财报交付命令。**飞轮命令以[数据飞轮运行说明](data-flywheel.md)和 `python -m workflow.flywheel_cli --help` 为准。**

本项目当前以本地练习为边界：原始文件、OCR 结果和派生文件落在本地目录，状态和身份信息写入 SQLite。当前不要求 PostgreSQL、MinIO、远程知识库或生产交付服务。

所有 Python 入口都支持 `-h` / `--help` 查看帮助。以下命令均应在项目根目录执行；没有特别说明时使用当前虚拟环境中的 `python`。

## 1. 安装、配置和测试

```bash
python -m pip install -r requirements.txt
cp .env.example .env
python -m unittest discover -s tests -v
python -m compileall core processing qc spiders storage remote
```

`.env` 只保存本机配置和密钥，已经被 `.gitignore` 忽略。没有 LLM Key 时，治理器默认使用确定性的 `mock` 路径；没有配置 OCR 服务时，学术采集器仍可只做元数据和 PDF 下载。

## 2. 手工 PDF 下载器：`spiders.downloader`

下载器只消费 JSONL 清单，不负责从网站搜索报告。最小示例：

```bash
python -m spiders.downloader \
  --input examples/reports.example.jsonl \
  --data-root data \
  --timeout 30 \
  --min-size 100 \
  --user-agent fin-doc-governance/0.1
```

命令行参数：

- `--input PATH`：必填；输入报告清单，每行一个 JSON 对象。
- `--data-root PATH`：本地数据根目录，默认 `data`。
- `--timeout SECONDS`：单次 HTTP 请求超时，默认 `30`。
- `--min-size BYTES`：接受 PDF 的最小字节数，默认 `100`；过小文件会进入失败/隔离流程。
- `--user-agent TEXT`：HTTP `User-Agent`，默认 `fin-doc-governance/0.1`。

每行 JSONL 支持以下字段：

- 必填：`stock_code`、`report_year`、`source_url`。
- 可选：`report_type`（`annual`、`semiannual`、`quarterly`、`research`，默认 `annual`）、`publish_date`、`title`、`source_name`、`source_id`、`candidate_uid`、`canonical_uid`、`source_priority`（整数，默认 `100`）。

示例：

```json
{"stock_code":"600519","report_year":2023,"report_type":"annual","publish_date":"2024-04-03","title":"贵州茅台2023年年度报告","source_url":"https://example.com/report.pdf","source_name":"cninfo","source_id":"1219506510","source_priority":10}
```

`stock_code` 会标准化为六位字符串；`source_url` 必须是 `http://` 或 `https://`。清单可以包含空行和以 `#` 开头的注释行。下载器会做临时文件原子落盘、PDF 头尾和页数检查、SHA-256 去重及重复 URL 跳过。

## 3. 财报多来源采集器：`spiders.collector`

```bash
python -m spiders.collector \
  --config config/collection.json \
  --data-root data \
  --batch-id batch-local-practice
```

只发现候选、不下载 PDF：

```bash
python -m spiders.collector \
  --config config/collection.json \
  --batch-id batch-local-practice \
  --dry-run
```

命令行参数：

- `--config PATH`：采集配置，默认 `config/collection.json`。
- `--data-root PATH`：覆盖配置文件顶层的 `data_root`；未指定时使用配置值，配置默认是 `data`。
- `--batch-id TEXT`：复用业务批次 ID；不传时自动生成。一次重跑应传相同值，以便关联同一批次下不同的 `run_id` 和 `attempt`。
- `--dry-run`：只发现候选，不下载文件；仍输出可供检查的发现结果。

当前配置启用 `cninfo` 和 `sse` 两个官方来源。财报 `targets[]` 的字段为：`stock_code`、`start_year`、`end_year`、`end_year_lag`、`report_type`。其中 `end_year` 可写具体年份或 `auto`/`current`；自动年份为当前年份减去 `end_year_lag`，默认滞后 1 年。`report_type` 支持 `annual`、`semiannual`、`quarterly`、`research`。

采集配置中的通用字段为：`data_root`、`min_pdf_size_bytes`；`http.timeout_seconds`、`http.min_interval_seconds`、`http.user_agent`；`retry.attempts`、`retry.backoff_seconds`；`sources[].name`、`sources[].enabled`、`sources[].priority`。来源名称由对应 adapter 实现，当前是 `cninfo` 和 `sse`。

## 4. 学术论文采集器：`spiders.scholarly_collector`

只发现并更新元数据：

```bash
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --data-root data \
  --batch-id batch-local-practice \
  --dry-run
```

下载开放获取 PDF，并限制本轮 OCR 数量：

```bash
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --batch-id batch-local-practice \
  --max-results 20 \
  --download-pdf \
  --ocr-backend local \
  --ocr-max-per-run 3
```

命令行参数：

- `--config PATH`：采集配置，默认 `config/collection.json`。
- `--data-root PATH`：覆盖配置文件顶层的 `data_root`。
- `--batch-id TEXT`：复用业务批次 ID；省略时自动生成。
- `--dry-run`：发现论文但不更新同步状态。
- `--max-results N`：覆盖所有 scholarly target 的 `max_results`，必须为正整数；不改变配置文件。
- `--download-pdf`：覆盖配置，下载来源明确标记为开放获取的 PDF。
- `--no-download-pdf`：覆盖配置，跳过开放获取 PDF 下载。
- `--ocr-backend {local,cloud}`：把已下载 PDF 送入配置好的 PaddleOCR 后端。`local` 使用本地/隧道 Serving，`cloud` 使用官方异步云端 API。
- `--ocr-max-per-run N`：限制本轮最多提交的 OCR 任务，必须为正整数；超出的记录标记为 `deferred`，下一轮继续。

`--download-pdf` 和 `--no-download-pdf` 互斥；不指定二者时使用 `scholarly.download_open_access_pdfs`。指定 `--ocr-backend` 时必须实际启用 PDF 下载。`cloud` 需要 `PADDLEOCR_CLOUD_TOKEN`；`local` 需要 `PADDLEOCR_LOCAL_API_URL` 或旧兼容变量 `PADDLEOCR_API_URL`。当前来源为 `crossref` 和 `openalex`。

`scholarly` 配置字段为：`enabled`、`download_open_access_pdfs`、`ocr_max_per_run`、`ocr_delay_seconds`、`sources[]` 和 `targets[]`。来源字段是 `name`、`enabled`、`priority`，Crossref 可用 `rows`，OpenAlex 可用 `per_page`。每个 target 的字段是 `name`、`query`、`start_year`、`end_year`、`end_year_lag`、`work_types`、`max_results`；`end_year` 支持具体年份、`auto` 或 `current`，`work_types` 可以是字符串或列表，`max_results` 范围为 `1` 到 `5000`。

## 5. 清洗、切片和 LLM 治理器：`processing.governance_runner`

```bash
python -m processing.governance_runner \
  --config config/collection.json \
  --data-root data \
  --batch-id batch-local-practice \
  --llm-backend mock
```

只预览待处理文档，不写治理产物：

```bash
python -m processing.governance_runner \
  --config config/collection.json \
  --batch-id batch-local-practice \
  --dry-run \
  --max-docs 2
```

命令行参数：

- `--config PATH`：采集配置，默认 `config/collection.json`。
- `--data-root PATH`：覆盖配置文件顶层的 `data_root`。
- `--batch-id TEXT`：复用业务批次 ID；不传时自动生成。
- `--dry-run`：只计划处理对象，不写治理 JSON、Markdown、chunk 和数据库派生记录。
- `--force`：即使已有治理完成标记，也重新处理文档。
- `--max-docs N`：本轮最多处理 N 份文档；省略表示不限制。
- `--llm-backend {auto,mock,openai,none}`：覆盖 `.env` 中的 `LLM_BACKEND`。`auto` 在有 `LLM_API_KEY` 时使用 OpenAI Python SDK，否则使用 mock；`mock` 不联网；`openai` 使用 SDK 的 Chat Completions JSON 模式，并读取 `.env` 中的 `LLM_API_URL`、`LLM_API_KEY` 和 `LLM_MODEL`；`none` 跳过 LLM。

治理器先执行确定性规则，再执行可选 LLM：规则负责噪声块过滤、Unicode/空白规范化、OCR 断行修复、重复块去重、表格 Markdown 化和语义切片；LLM 负责文档元数据补全、摘要、主题、质量评分以及表格摘要。原始 OCR `result.json` 和 `output.md` 不覆盖。

## 6. 一键本地流水线：`scripts/run_collection.sh`

```bash
cp .env.example .env
chmod +x scripts/run_collection.sh
FIN_DOC_BATCH_ID=batch-local-practice ./scripts/run_collection.sh
```

脚本现在转交 `workflow.flywheel_cli run`，支持 `--batch-id` 和 `--no-discover`，串接财报/学术来源、OCR、图表、治理、模型审核及预训练发布。`FIN_DOC_BATCH_ID` 仍可指定批次。启动前配置视觉/审核模型、tokenizer 和来源训练准入并执行预检，详见 [飞轮运行说明](data-flywheel.md)。

## 7. 本地 PaddleOCR Serving 客户端：`remote.paddle_client`

本地或 SSH 隧道服务已启动时：

```bash
python -m remote.paddle_client \
  --api-url http://127.0.0.1:18080/layout-parsing \
  --input data/raw_pdfs/example.pdf \
  --output-root data/parsed_md \
  --timeout 900 \
  --page-batch-size 20 \
  --token "$PADDLEOCR_LOCAL_TOKEN"
```

命令行参数：

- `--api-url URL`：Serving 的 `/layout-parsing` 地址；默认读取 `PADDLEOCR_LOCAL_API_URL`，也兼容 `PADDLEOCR_API_URL`。如果都没有设置则报错。
- `--token TEXT`：可选 Token；默认读取 `PADDLEOCR_LOCAL_TOKEN`，也兼容 `PADDLEOCR_TOKEN`。
- `--input PATH`：必填；一个 PDF 文件，或递归扫描 PDF 的目录。
- `--output-root PATH`：必填；派生结果根目录。
- `--timeout SECONDS`：请求超时，默认读取 `PADDLEOCR_LOCAL_TIMEOUT_SECONDS`，默认值 `900`。
- `--page-batch-size N`：每次发送给 Serving 的最大页数，默认读取 `PADDLEOCR_LOCAL_PAGE_BATCH_SIZE` / `20`。这是单请求大小，不是 PDF 总页数上限；例如 127 页 PDF 会按 `20+20+20+20+20+20+7` 请求。

输入为目录时，每个 PDF 输出到 `<output-root>/<文件名不含扩展名>/`。客户端按页面批次请求本地 Serving，成功段立即保存到 `segments/segment-xxxx/result.json`，并在 `ocr_segments.json` 记录页码范围、状态、错误和请求身份。全部页面成功且覆盖完整时，才生成供治理层使用的合并 `result.json` 和 `output.md`；单段失败会继续请求剩余段，但不会生成不完整的文档结果。重跑同一个输出目录会复用已成功的段，只重试失败/缺失段。客户端发送 Base64 文件，并兼容返回的 Base64、Data URI 和图片 URL。

## 8. 官方 PaddleOCR 云端客户端：`remote.paddle_cloud_client`

提交本地文件：

```bash
python -m remote.paddle_cloud_client \
  --input data/raw_pdfs/example.pdf \
  --output-root data/parsed_md/cloud \
  --job-url "$PADDLEOCR_CLOUD_JOB_URL" \
  --token "$PADDLEOCR_CLOUD_TOKEN" \
  --model PaddleOCR-VL-1.6 \
  --poll-interval 5 \
  --timeout 1800 \
  --submit-retries 3 \
  --submit-backoff 10
```

也可以提交公网可访问的 PDF URL：

```bash
python -m remote.paddle_cloud_client \
  --file-url https://example.com/report.pdf \
  --token "$PADDLEOCR_CLOUD_TOKEN"
```

客户端仅对任务提交和状态轮询使用 Bearer Token。完成任务后官方返回的 `resultUrl` 是带临时
授权参数的 BOS 短链，JSONL 与其中引用的图片会匿名下载；请勿在这些链接上额外传递
Bearer Token，否则会触发 BOS 的 `MissingDateHeader` 错误。

参数：

- `--input PATH`：本地 PDF；与 `--file-url` 二选一且必须提供一个。
- `--file-url URL`：公网可访问的 PDF URL；与 `--input` 二选一。
- `--output-root PATH`：派生结果根目录，默认 `data/parsed_md/cloud`。
- `--job-url URL`：云端任务接口，默认 `PADDLEOCR_CLOUD_JOB_URL` 中的地址。
- `--token TEXT`：云端 Token，默认 `PADDLEOCR_CLOUD_TOKEN`；必填，不能提交到代码或 Git。
- `--model TEXT`：模型名，默认 `PADDLEOCR_CLOUD_MODEL`，再默认到 `PaddleOCR-VL-1.6`。
- `--poll-interval SECONDS`：轮询间隔，默认 `PADDLEOCR_CLOUD_POLL_INTERVAL_SECONDS` / `5`。
- `--timeout SECONDS`：提交、轮询和结果下载的超时，默认 `PADDLEOCR_CLOUD_TIMEOUT_SECONDS` / `1800`。
- `--submit-retries N`：队列繁忙时的提交次数，默认 `PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS` / `3`。
- `--submit-backoff SECONDS`：队列繁忙重试的指数退避基数，默认 `PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS` / `10`。

## 9. 运行时环境变量

以下列出传统采集、OCR 和治理入口的主要变量；每日飞轮的模型角色、预算与 tokenizer 变量见[飞轮运行说明](data-flywheel.md)，完整模板见[.env.example](https://github.com/Chal1ce/FinFlow/blob/main/.env.example)。路径均可使用 `~`；真实 shell 环境变量优先于 `.env`。

数据路径：

- `FIN_DOC_DATA_ROOT`：数据根目录，默认 `data`。
- `FIN_DOC_RAW_PDFS`：原始 PDF 目录，默认 `$FIN_DOC_DATA_ROOT/raw_pdfs`。
- `FIN_DOC_PARSED_MD`：OCR 解析目录，默认 `$FIN_DOC_DATA_ROOT/parsed_md`。
- `FIN_DOC_PROCESSED`：加工根目录，默认 `$FIN_DOC_DATA_ROOT/processed`。
- `FIN_DOC_GOVERNED`：治理结果目录，默认 `$FIN_DOC_PROCESSED/governed`。
- `FIN_DOC_CHUNKS_DIR`：按文档保存的 chunk 目录，默认 `$FIN_DOC_PROCESSED/chunks`。
- `FIN_DOC_CHUNKS_JSONL`：汇总 chunk JSONL，默认 `$FIN_DOC_PROCESSED/chunks.jsonl`。
- `FIN_DOC_MANIFESTS`：Manifest 目录，默认 `$FIN_DOC_DATA_ROOT/manifests`。
- `FIN_DOC_STATE_DB`：SQLite 状态库，默认 `$FIN_DOC_DATA_ROOT/state/pipeline.db`。
- `FIN_DOC_BATCH_ID`：一键脚本和飞轮使用的业务批次 ID。
- `FIN_DOC_USER_AGENT`：HTTP 和 OCR 客户端的 User-Agent，默认 `fin-doc-governance/0.1`。

本地 PaddleOCR：

- `PADDLEOCR_LOCAL_API_URL`：本地 Serving 地址。
- `PADDLEOCR_LOCAL_TOKEN`：本地 Serving Token，可为空。
- `PADDLEOCR_LOCAL_TIMEOUT_SECONDS`：本地客户端超时，默认 `900`。
- `PADDLEOCR_LOCAL_PAGE_BATCH_SIZE`：单次本地 Serving OCR 请求的最大 PDF 页数，默认 `20`；不限制每轮文档数量，也不限制单份 PDF 总页数。
- `PADDLEOCR_LOCAL_OPTIONS`：JSON 对象，覆盖本地 Serving 的可选能力参数。

官方云端 PaddleOCR：

- `PADDLEOCR_CLOUD_TOKEN`：云端 Token，无默认值；使用 `--ocr-backend cloud` 或云端客户端时必填。
- `PADDLEOCR_CLOUD_JOB_URL`：异步任务接口，默认 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`。
- `PADDLEOCR_CLOUD_MODEL`：模型，默认 `PaddleOCR-VL-1.6`。
- `PADDLEOCR_CLOUD_POLL_INTERVAL_SECONDS`：轮询间隔，默认 `5`。
- `PADDLEOCR_CLOUD_TIMEOUT_SECONDS`：云端超时，默认 `1800`。
- `PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS`：队列满时提交重试次数，默认 `3`。
- `PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS`：指数退避基数，默认 `10` 秒。
- `PADDLEOCR_CLOUD_OPTIONAL_PAYLOAD`：JSON 对象，默认关闭文档方向、去畸变和图表识别。

治理和 LLM：

- `FIN_DOC_RULE_VERSION`：规则版本，默认 `ocr-cleaning-v3`；参与 `governed_uid` 计算。
- `FIN_DOC_DROP_BLOCK_LABELS`：JSON 字符串数组，默认 `header`、`footer`、`number`、`image`、`chart`、`vision_footnote`。
- `FIN_DOC_CHUNK_MAX_CHARS`：切片目标最大字符数，默认 `2000`。
- `FIN_DOC_CHUNK_MIN_CHARS`：普通内容块的最小字符数，默认 `80`。
- `LLM_BACKEND`：`auto`、`mock`、`openai` 或 `none`，默认 `auto`。
- `LLM_API_URL`：可选，传给 OpenAI SDK 的 `base_url`，例如 `https://api.openai.com/v1` 或兼容服务的根地址；不设置时使用 SDK 默认的官方 OpenAI 地址。
- `LLM_API_KEY`：传给 OpenAI SDK 的 API Key；使用 `openai` 时必填。
- `LLM_MODEL`：模型名称，默认 `gpt-4o-mini`。
- `LLM_TIMEOUT_SECONDS`：LLM 请求超时，默认 `60`。
- `LLM_RETRY_ATTEMPTS`：LLM 请求重试次数，默认 `2`。

兼容旧变量：

- `PADDLEOCR_API_URL`：仅在 `PADDLEOCR_LOCAL_API_URL` 未设置时作为本地地址。
- `PADDLEOCR_TOKEN`：仅在 `PADDLEOCR_LOCAL_TOKEN` 未设置时作为本地 Token。
- `PYTHON_BIN`：在启动脚本的 shell 中显式指定 Python 命令（例如 `export PYTHON_BIN=python3`）；脚本选择解释器时尚未加载 `.env`。未设置时优先项目虚拟环境，再使用系统 `python3`。

JSON 类型的环境变量必须是合法 JSON，例如：

```bash
FIN_DOC_DROP_BLOCK_LABELS='["header","footer","number","image","chart"]'
PADDLEOCR_LOCAL_OPTIONS='{"useDocOrientationClassify":true,"useTableRecognition":true}'
```

## 10. SQLite 状态和血缘查询

默认数据库为 `data/state/pipeline.db`，如果修改了 `FIN_DOC_STATE_DB`，下面命令中的路径也要相应替换。先查看表：

```bash
sqlite3 data/state/pipeline.db '.tables'
sqlite3 data/state/pipeline.db '.schema pipeline_batch'
```

批次和运行：

```sql
SELECT batch_id, status, last_run_id, created_at, finished_at
FROM pipeline_batch
ORDER BY created_at DESC;

SELECT run_id, batch_id, attempt, status, dry_run, started_at, finished_at, error_msg
FROM pipeline_run
ORDER BY started_at DESC;

SELECT run_id, batch_id, step_name, entity_uid, attempt, status, error_msg
FROM pipeline_step
ORDER BY started_at DESC
LIMIT 50;
```

文档、文件资产和派生 artifact：

```sql
SELECT document_uid, document_type, title, source_name, status, updated_at
FROM document_registry
ORDER BY updated_at DESC;

SELECT artifact_uid, artifact_type, document_uid, asset_uid, governed_uid,
       parent_artifact_uid, path, sha256, status
FROM artifact
ORDER BY updated_at DESC;

SELECT governed_uid, document_uid, asset_uid, backend, rule_version,
       input_path, output_path, status, block_count, char_count
FROM governed_document
ORDER BY updated_at DESC;
```

chunk 和 QC：

```sql
SELECT chunk_id, chunk_version_uid, chunk_uid, document_uid, governed_uid,
       chunk_index, page, content_type, title_context, text_hash, status
FROM document_chunk
ORDER BY document_uid, chunk_index;

SELECT chunk_id,
       json_extract(metadata_json, '$.table') AS table_text,
       json_extract(metadata_json, '$.table_source') AS table_source,
       json_extract(metadata_json, '$.context_text') AS context_text,
       json_extract(metadata_json, '$.retrieval_text') AS retrieval_text,
       json_extract(metadata_json, '$.llm_enrichment_uid') AS llm_enrichment_uid
FROM document_chunk
WHERE json_extract(metadata_json, '$.table') IS NOT NULL;

SELECT qc_stage, check_name, status, entity_uid, message, created_at
FROM qc_result
ORDER BY created_at DESC
LIMIT 50;

SELECT governance_uid, entity_uid, stage, model, model_version, status, error_msg
FROM llm_governance
ORDER BY created_at DESC
LIMIT 50;
```

身份关系可以按以下方向追溯：`batch_id → run_id → document_uid → asset_uid → governed_uid → chunk_id/chunk_version_uid`。文件名、标题和路径是定位字段，不是唯一身份；`artifact.parent_artifact_uid` 保存直接父产物关系。

## 11. 常见重跑方式

复用同一批次继续失败任务：

```bash
python -m spiders.collector --config config/collection.json --batch-id batch-local-practice
python -m spiders.scholarly_collector --config config/collection.json --batch-id batch-local-practice
python -m processing.governance_runner --config config/collection.json --batch-id batch-local-practice
```

只重建治理派生层：

```bash
python -m processing.governance_runner \
  --config config/collection.json \
  --batch-id batch-local-practice \
  --force \
  --llm-backend mock
```

规则升级时先修改 `FIN_DOC_RULE_VERSION`，再使用 `--force` 重跑治理。原始 PDF、原始 OCR JSON/Markdown 和旧治理版本不会被覆盖；同一输入重复执行通过唯一键、upsert、完成标记和原子写入保持幂等。

## 12. 采集财报进入本地交付流程

`spiders.collector` 成功下载的财报会在 `data/manifests/reports.jsonl` 中记录
`batch_id` 和 `batch_ids`。这让同一个不可变 PDF 可以安全复用于多个业务批次。
使用 `collected_financial` 流水线可直接接管一个已采集批次，保留原始来源 URL、
来源 ID、下载路径和 SHA-256，再完成 OCR、质量提示、元数据规范化、治理和本地交付：

```powershell
python -m spiders.collector --config config/collection.json --batch-id fin-2026-08-11
python -m workflow.cli run --pipeline collected_financial `
  --batch-id fin-2026-08-11 `
  --ocr-backend local `
  --llm-backend mock
```

`spiders.collector` 和 `workflow.cli run --pipeline collected_financial` 都接受
`--batch-id`，但必须使用同一个值：采集时用它把本轮下载的报告关联到批次，工作流时
用它接管并处理同一批次。未显式指定时，采集器会自动生成
`batch-<UTC时间>-<进程号>`（`scripts/run_collection.sh` 也使用 `FIN_DOC_BATCH_ID`
或相同的生成规则），并记录在运行摘要 JSON、`data/manifests/reports.jsonl` 和
SQLite 中。

确认已有批次后，把 `batch_id` 原样传给工作流即可：

```bash
# 查看最近的批次和运行状态
python -m workflow.cli overview

# 或直接查询 SQLite
sqlite3 data/state/pipeline.db \
  "select batch_id, status, last_run_id, created_at from pipeline_batch order by rowid desc limit 10;"

python -m workflow.cli run \
  --pipeline collected_financial \
  --batch-id <batch_id> \
  --ocr-backend local \
  --llm-backend mock
```

## 12.1 远端 OCR 完整运行

工作流中的 `--ocr-backend local` 指项目通过 `.env` 的
`PADDLEOCR_LOCAL_API_URL` 连接的自建远端 PaddleX Serving（通常经 SSH 隧道暴露在
`http://127.0.0.1:18080/layout-parsing`）；`--ocr-backend cloud` 指官方异步云端
API，需要先在 `.env` 中配置 `PADDLEOCR_CLOUD_TOKEN`。

对已采集批次完整跑一次远端 OCR 和本地交付：

```bash
python -m workflow.cli run \
  --pipeline collected_financial \
  --batch-id <batch_id> \
  --ocr-backend local \
  --llm-backend auto
```

只处理单个 PDF、不进入工作流时，直接调用远端 Serving 客户端：

```bash
python -m remote.paddle_client \
  --api-url "$PADDLEOCR_LOCAL_API_URL" \
  --input data/raw_pdfs/2024/600519/600519_2024_annual.pdf \
  --output-root data/parsed_md
```

使用官方云端 API 时，把上面的 `--ocr-backend local` 换成
`--ocr-backend cloud`；Token 只放在 `.env`，不要写进命令行。直接客户端、SSH
隧道和云端参数的完整说明见 [PaddleOCR 部署与连接](paddleocr_remote.md)。

没有关联到成功财报的批次会返回 `skipped` 并以退出码 `0` 结束，不会误报为失败。
该流水线不要求把下载文件再复制一遍：本地导入清单直接引用已校验的原始 PDF。

定时脚本已升级为每日飞轮，使用 `FIN_DOC_FLYWHEEL_OCR_BACKEND=local|cloud`，并自动接入新文档 OCR 和预训练候选。需要填写模型、tokenizer 与来源准入后再启用定时；旧的 `FIN_DOC_COLLECTED_OCR_BACKEND` 不再控制脚本。独立 `collected_financial` 流水线仍保留。

## 13. 本地运行与恢复能力

SQLite 状态库启用了 WAL、完全同步和 5 秒忙等待，适合单机上同时运行查看命令和一个
执行任务。以下命令不需要数据库服务、对象存储或容器：

```powershell
# 最近批次、失败/执行中的工作流、待复核数量和各批次最新交付
python -m workflow.cli overview

# 按批次或状态查看工作流历史
python -m workflow.cli runs --limit 20 --status failed

# 对不可变交付包重新计算并核对 checksums.sha256
python -m workflow.cli verify-release --batch-id fin-2026-08-11 --release-id <release_id>

# 使用 SQLite 在线备份生成可独立打开的数据库快照
python -m workflow.cli backup-state
python -m workflow.cli backup-state --output .\data\backups\before-upgrade.sqlite3
```

校验命令会拒绝格式错误、重复或越界的校验清单条目；内容被修改、缺失或哈希不匹配时
返回非零退出码。备份不会覆盖现有文件，并在写入前执行 SQLite 完整性检查。

## 14. 可复现开发检查

生产运行只需要 `requirements.txt`；本地开发和 CI 可以安装包含测试、静态检查工具的
可选依赖：

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m ruff check .
```
