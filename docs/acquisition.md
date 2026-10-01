---
title: 数据来源与采集
nav_order: 1
parent: 数据处理
---

# 数据来源与采集
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本页用于单独运行下载器和采集器。每日自动采集及后续处理使用[数据飞轮](data-flywheel.md)，其分页窗口、队列和预算策略以飞轮说明为准。

## 手工 PDF 下载

安装下载器运行所需依赖：

```bash
python -m pip install -r requirements.txt
```

下载器采用“报告清单驱动”方式，不负责从网站搜索报告。先把报告的下载地址和基础元数据写成 JSONL，再执行下载：

```bash
python -m spiders.downloader \
  --input examples/reports.example.jsonl \
  --data-root data
```

示例清单中的地址来自巨潮资讯官方PDF。建议第一阶段使用巨潮资讯或交易所官方披露文件，遵守网站的访问规则，控制请求频率。

运行后会生成：

```text
data/
├── raw_pdfs/{年份}/{股票代码}/*.pdf
├── quarantine/{年份}/{股票代码}/*.pdf
└── manifests/reports.jsonl
```

下载器具备以下能力：

- 临时文件下载完成后原子落盘，避免留下半个 PDF；
- SHA-256 文件指纹去重；
- 基础 PDF 头、尾标记和页数检查；
- 同一 URL 重跑跳过；
- 不同 URL 下载到相同内容时复用原文件；
- 无效文件进入 `quarantine`；
- JSONL 清单记录来源、文件路径、Hash、页数和状态。

运行测试：

```bash
python -m unittest discover -s tests -v
```

远程 PaddleOCR 部署、本地 Serving 客户端和官方云端 API 客户端见
[docs/paddleocr_remote.md](paddleocr_remote.md)。

本地 Serving 客户端入口为 [remote/paddle_client.py](https://github.com/Chal1ce/FinFlow/blob/main/remote/paddle_client.py)，官方云端异步 API
入口为 [remote/paddle_cloud_client.py](https://github.com/Chal1ce/FinFlow/blob/main/remote/paddle_cloud_client.py)。运行时配置集中在
[config.py](https://github.com/Chal1ce/FinFlow/blob/main/config.py)，云端 Token 只能通过 `PADDLEOCR_CLOUD_TOKEN` 环境变量提供。

## 多来源自动采集

配置文件是 [config/collection.json](https://github.com/Chal1ce/FinFlow/blob/main/config/collection.json)，当前启用巨潮资讯和上海证券交易所两个官方来源。`end_year: auto` 会按当前年份自动采集上一报告年度。采集器会先发现候选报告，再按来源优先级下载；主来源失败时自动尝试备用来源。

手动执行一次：

```bash
python -m spiders.collector --config config/collection.json
```

只发现、不下载：

```bash
python -m spiders.collector --config config/collection.json --dry-run
```

生成内容位于：

```text
data/
├── discovery/candidates.jsonl
├── runs/{run_uid}.json
├── manifests/reports.jsonl
└── state/pipeline.db
```

`state/pipeline.db` 保存批次、运行、步骤、文档注册表、原始资产、派生 artifact、chunk 和 QC 记录；`candidates.jsonl`、Manifest 和运行摘要保留为方便人工检查的可读产物。

当前本地练习使用分层身份：`batch_id` 表示可复用的业务批次，`run_id` 表示该批次的一次执行尝试，`report_uid/work_uid` 表示逻辑文档，`asset_uid` 表示不可变文件资产，`governed_uid` 表示某个规则版本下的治理产物，`chunk_id` 表示逻辑内容块，`chunk_version_uid/chunk_uid` 表示该治理版本中的具体 chunk，`candidate_uid` 表示某个来源候选。文件名、标题和路径只用于定位，不作为唯一身份。PDF 版本变化时，逻辑文档可以保持不变，新的文件只产生新的资产和治理版本身份。

如果任务中途失败，可以复用同一个批次 ID：

```bash
python -m processing.governance_runner \
  --config config/collection.json \
  --batch-id batch-local-practice
```

再次执行时会创建新的 `run_id` 和递增的批次 `attempt`，已存在 `.complete` 的文档会跳过，失败文档会继续处理。`scripts/run_collection.sh` 现在调用每日飞轮，统一采集与后续处理的批次 ID；也可以通过 `FIN_DOC_BATCH_ID` 指定批次。

QC分为候选元数据QC和原始文件QC，结果可直接查询：

```bash
sqlite3 data/state/pipeline.db \
  'select qc_stage, check_name, status, message from qc_result order by created_at desc limit 20;'
```

## 学术论文与期刊来源

配置文件中的 `scholarly` 区块已经启用 Crossref 和 OpenAlex，并默认下载明确标记为开放获取的 PDF。它们负责发现论文元数据，使用 DOI 或 OpenAlex ID 建立稳定身份，并通过 `source_sync_state` 保存每个来源和查询目标的同步时间；重复执行时只处理新增或元数据发生变化的记录。

手动执行一次：

```bash
python -m spiders.scholarly_collector --config config/collection.json
```

临时限制每个来源最多返回 `20` 条，不修改配置文件：

```bash
python3 -m spiders.scholarly_collector \
  --config config/collection.json \
  --max-results 20
```

持久限制则在 `config/collection.json` 的 `scholarly.targets[].max_results` 中修改。`max_results` 是每个来源、每个查询目标的分页预算；为完整保存每页记录，实际返回数量可能多出不足一页。同时启用 Crossref 和 OpenAlex 时，候选数约为两者之和。

只下载开放获取 PDF、不执行 OCR：

```bash
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --download-pdf
```

下载后送入本地 PaddleOCR Serving：

```bash
export PADDLEOCR_LOCAL_API_URL="http://127.0.0.1:18080/layout-parsing"
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --ocr-backend local
```

或送入官方云端 API：

```bash
export PADDLEOCR_CLOUD_TOKEN="新Token"
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --ocr-backend cloud
```

只发现、不更新同步状态：

```bash
python -m spiders.scholarly_collector --config config/collection.json --dry-run
```

产物位于：

```text
data/
├── discovery/scholarly_candidates.jsonl
├── raw_pdfs/scholarly/{年份}/{work_uid}/*.pdf
├── named_pdfs/scholarly/{年份}/{0,1,2,...}.pdf
├── quarantine/scholarly/{work_uid}/*.pdf
├── parsed_md/scholarly/{work_uid}/{asset_uid}/{backend}/
├── runs/*-scholarly.json
├── manifests/scholarly_documents.jsonl
└── state/pipeline.db
```

候选中的 `source_url` 是论文落地页，`pdf_url` 仅在来源提供开放获取 PDF 时存在。受版权或登录限制的论文只保存元数据，不自动绕过访问控制下载 PDF。

原始 PDF 始终保留在 `raw_pdfs/scholarly` 中，不会因重命名被覆盖；下载器会在 `named_pdfs/scholarly` 生成从 `0.pdf` 开始递增编号的 PDF 副本，序号持久化在 manifest 中，同一 PDF 内容会复用同一编号，方便直接用作 OCR 或人工核对输入。

送入 OCR 的一律使用 `named_pdfs/scholarly` 中的数字副本，而不是直接读取原始文件。每次 OCR 前，采集器会在对应 `parsed_md/scholarly/{work_uid}/{asset_uid}/{backend}/input.json` 写入本次输入的 `run_id`、`candidate_uid`、`raw_path`、`named_path`、`named_number`、来源信息和输出目录；`discovery/scholarly_candidates.jsonl` 也会记录 `pdf_named_path` 与 `ocr_input_path`，因此从源站链接、原始 PDF、数字副本到 OCR 输出可以逐级回溯。

PDF 下载失败会被分类记录：`403/401/407` 在带落地页 Referer 和浏览器 User-Agent 重试一次后仍失败时标记为 `blocked`，并写入 manifest；同一 URL 和来源更新时间不变时不会反复下载，避免每次 cron 都重试同一个被封锁链接。`429/5xx` 和网络类错误会按 `retry.pdf_attempts` 退避重试；OCR 云端任务队列满（`code 10010`）会自动重试，并在本轮仍未成功后把运行状态记为 `partial`，下一次 cron 会继续处理未同步的候选。只有本轮没有任何成功下载或成功 OCR、并且存在永久失败（如 `blocked`、无效 PDF）时，运行状态才保持 `failed`；只要有一份成功产出，就会记为 `partial`，避免单个被封锁来源导致整个 cron 失败。

当前配置会在一轮中提交全部待处理 PDF。`config/collection.json` 的 `scholarly.ocr_max_per_run` 控制每轮最多提交的 OCR 数量；设置为 `null` 表示不限制数量，设置为正整数时，超出部分会在运行摘要中标记为 `deferred`，且不推进同步状态，下一轮运行会继续处理。`scholarly.ocr_delay_seconds` 控制两次 OCR 提交之间的最小间隔。临时调整也可以使用 `--ocr-max-per-run 3`。

云端提交队列的重试次数和退避间隔可以通过 `.env` 调整：

```bash
PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS=3
PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS=10
```

如果暂时不希望下载 PDF，可以使用 `--no-download-pdf`；未配置 OCR 服务时，采集器只完成元数据和原始 PDF 层，不会主动调用 OCR。
