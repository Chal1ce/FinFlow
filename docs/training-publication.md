---
title: 每日训练版本与恢复
nav_order: 5.7
---

# 每日训练版本、增量恢复与日报

每日流程可继续执行：**采集 → OCR/治理 → CPT/SFT → 累计快照 → 近似去重 → 配比 → 覆盖报告 → 训练版本发布**。
统一在项目根目录的 `.env` 配置，凭证、模型设置与发布参数放在同一个文件。默认关闭最终训练发布和 SFT；原来的增量生成仍可独立使用。

## 1. 配置一个 .env

已有 `.env` 时只补字段，不要覆盖。操作系统环境变量优先于 `.env`；修改后重启长期运行的渠道进程。
Python 把 `.env` 当数据解析，不要用 shell `source` 执行含 JSON 的配置。

```dotenv
FIN_DOC_RELEASE_ENABLED=true
FIN_DOC_RELEASE_NEAR_DEDUP=true
FIN_DOC_REPORT_TIMEZONE=Asia/Shanghai
FIN_DOC_SFT_ENABLED=false

# 覆盖 flywheel.json 对应的顶层字段；未写字段继续使用模板默认值。
# source_policy 是整个对象替换，请包含所有需要保留的来源。
FIN_DOC_FLYWHEEL_POLICY={"methods":["original","visual"],"source_policy":{"your-source":{"training":"approved","usage_scope":"internal-only"}}}

# budget:null 按去重后训练池的总 token / 样本数设置目标。
FIN_DOC_RELEASE_CPT_MIXTURE={"method":"temperature","alpha":1.0,"group_fields":["source_name"],"unit":"tokens","budget":null,"seed":42,"max_per_family":null,"redistribute":true,"allow_shortfall":false}
FIN_DOC_RELEASE_SFT_MIXTURE={"method":"temperature","alpha":1.0,"group_fields":["task"],"unit":"samples","budget":null,"seed":42,"max_per_family":null,"redistribute":true,"allow_shortfall":false}
FIN_DOC_RELEASE_NEAR_OPTIONS={"threshold":0.9,"ngram":5,"num_perm":64,"bands":16,"min_chars":80,"max_candidates":10000,"max_chars":200000}
FIN_DOC_RELEASE_COVERAGE_TARGETS=[{"field":"method","label":"original","min_samples":100}]
```

把 `your-source` 换为实际来源；仅在确认可用于训练后批准。模型和 tokenizer 仍使用 `.env.example` 中的原有环境变量。
JSON 值写在一行，布尔值使用 `true/false`。配方模板仍是版本控制下的默认值，日常使用无需再改多个 JSON 文件。

启用 SFT 时，在同一个文件补上对应模型与：

```dotenv
FIN_DOC_SFT_ENABLED=true
# 可选，默认 config/sft.json
FIN_DOC_SFT_CONFIG=config/sft.json
FIN_DOC_SFT_POLICY={"tasks":["document_qa","extraction","visual_qa"],"strategies":["direct"],"max_jobs":20,"max_model_requests":40}
```

`visual_qa` 要配置视觉生成和视觉审核角色。`FIN_DOC_SFT_POLICY` 同样按顶层字段覆盖，适用于独立 SFT CLI 和每日流程。
`FIN_DOC_FLYWHEEL_POLICY` 可覆盖采集配置路径、治理方式、重试、split 等原有策略字段；既有专用环境变量仍有更高优先级。
更改去重/配比参数只影响发布阶段，不会使上游生成缓存整体失效。

### 配比规则

- 支持 `temperature` 和 `fixed`；fixed 使用 `weights` 对象，键必须和输入池的实际分组一致。
- CPT 可按训练 tokenizer 的 tokens 或 samples 配比；SFT 按 samples。没有供应商费用估算。
- 不重复采样；完整保留 validation，不对其近似去重或配比。
- `budget:null` 默认配合 `alpha:1`、无家族上限和 `redistribute:true` 使用全部可用训练数据。
- 配额、完整样本粒度、家族上限可能产生缺口；默认保留配比候选包和诊断，但不更新正式版本。
- `allow_shortfall:true` 显式允许有缺口的非空版本成为 latest；缺口仍写入日报和版本报告。
- DoReMi/RegMix 继续在冻结输入池上显式实验；每日流程不启动代理训练，也不接受与每天变化的池不匹配的旧权重。

## 2. 执行与恢复

```sh
python -m workflow.flywheel_cli preflight
python -m workflow.flywheel_cli run

# 仅恢复汇总、去重、配比和发布，不采集、不调用 OCR/模型。
python -m workflow.flywheel_cli publish-training

# 如果独立生成了 SFT，可显式选择已完成、同配方且当前仍获准的快照。
python -m workflow.flywheel_cli publish-training --sft-dataset data/training/sft/datasets/sft-example

python -m workflow.flywheel_cli status
python -m workflow.flywheel_cli report --date 2026-10-04
```

`publish-training` 仍需同一数据目录、来源策略、当前 tokenizer 文件与相应本地依赖；SFT 配方身份包含模型配置。
定时器继续调用已有 `scripts/run_flywheel.sh`，无需注册第二个任务。项目不会自动安装系统定时器。

### 数据库与血缘

复用 `state/pipeline.db` 的 `flywheel_record`、`artifact` 和 `artifact_edge`：

| 记录类型 | 用途 |
| --- | --- |
| `training-input` | 已校验的输入包身份及其候选/样本父工件 |
| `training-stage` | 快照、去重报告、配比、报告和发布的输入身份、尝试次数、状态及产物哈希 |
| `training-index` | 某输入包在专用近似去重索引中的登记 |
| `training-export-sample` | 最终导出样本 ID 到原始候选/SFT 样本血缘 |
| `training-release` / `training-latest` | 不可变版本记录与最新指针的数据库投影 |
| `daily-start` / `daily` | 每轮开始库存和结束报告，用于中断识别与按日汇总 |

阶段键由输入包校验清单和配方决定。重跑先校验已有产物，复用成功步骤；文件目录原子改名完成但数据库尚未登记时，补记血缘。
已登记产物缺失或哈希改变会停止发布，不能把损坏包当作成功缓存。数据库和本地文件应一并备份。
CPT 汇总当前 tokenizer/分块配方的全部历史增量与血缘增量；SFT 使用选定证据包的累计快照，不拼接每日 SFT 快照。
同证据、同生成配方、同来源策略的成功 SFT 导出直接复用；部分完成的生成在下一轮用已有任务/响应缓存续作。

近似索引按类型、配方和去重参数隔离，保留跨版本指纹。历史匹配不在当前池时只报告；自动排除需要当前池存在直接相似的保留代表。
见[跨版本近似去重](near-dedup.md)。

证据版本身份只依赖上游候选/决定/视觉资产/治理工件，新增日报、SFT 或最终发布工件不会反过来触发新的证据版本。
任务时限在阶段边界检查；正在进行的快照校验、复制或 MinHash 计算不强行中断，单阶段可能超过软时限。

## 3. 不可变版本与 latest

```text
data/
  training/pretrain/                 # 原始每日 CPT 增量，保留
  training/origin_deltas/            # 新来源血缘增量
  training/snapshots/daily-<hash>/    # 累计 CPT 快照
  training/mixtures/cpt-<hash>/       # 发布前选择结果（可能存在配额缺口）
  training/releases/release-<hash>/
    manifest.json
    checksums.sha256
    cpt/                            # 可直接交给下游的配比包
    sft/                            # 启用时包含文本、视觉 JSONL 和原图
    reports/cpt/                    # JSON / Markdown 覆盖与版本变化
    reports/sft/
  training/latest.json
  indexes/daily-<kind>-<recipe>.sqlite
  reports/daily/YYYY-MM-DD.json
  reports/daily/YYYY-MM-DD.md
```

```sh
python -m workflow.flywheel_cli verify-training data/training/releases/release-实际哈希
python -m workflow.flywheel_cli trace --sample-id 最终导出文件中的sample_id
```

版本包包含独立可校验的 CPT/SFT 配比包及报告；训练文件/原图可整体搬走，完整历史血缘查询仍需要原数据库与上游数据。
报告与 manifest 中的原始路径用于审计，不要求下游加载器沿原机器路径读取数据。
同输入、同配方复用版本，不产生新的训练版本；历史版本不覆盖、不删除。

`latest.json` 只在完整版本及嵌套数据/报告校验通过后原子替换。SFT 未完成、空训练选择、默认不允许的配额缺口、哈希损坏都保留上一版。
文件指针是发布提交点；进程在指针替换后、数据库更新前退出时，下一次会根据校验通过的指针修复数据库投影。
当前候选或来源的训练批准已撤回时，历史 CPT 汇总会阻止发布，要求先重建经过审核的语料；历史版本不会被自动追溯删除。
启用 SFT 时，CPT/SFT 作为同一版本一起提交；原始 CPT 日增量仍独立保留，不受最终发布失败影响。

## 4. 完整日报

每轮 JSON 保存在 `manifests/daily/<run_id>.json`，按 `FIN_DOC_REPORT_TIMEZONE` 汇总到 `reports/daily/YYYY-MM-DD.{json,md}`。
`report` CLI、飞书/Telegram/Discord/Slack 的日报命令和 MCP 共用按日聚合口径。

日报包括：

- 完成任务、运行状态、错误类型、队列积压和审核状态；模型请求数及原始 usage（不换算费用）。
- 数据库新增候选、图片/表格、CPT 样本/来源、SFT 样本；恢复时补登记也计为新增，不代表新调用模型。
- 每个阶段的 built/reused/indexed、耗时与路径；发布失败阶段和机器原因代码。
- 当前版本、输入池样本数、近似去重排除数、选入 train/validation、目标/实际配额、分组比例和缺口。
- 版本包的报告提供覆盖目标缺口及相对上一版本的内容增减；tokenizer 改变时不做跨 tokenizer 的 token 差分。

当同日运行多次，只累计任务、请求及数据库新增量；候选/队列/版本大小使用最新快照，避免重复计数。
异常退出尽量落盘失败报告；强制终止后由下一次持锁运行补记 interrupted。强制终止前未持久化的请求数/任务计数无法恢复，会标记不可用。
未实现独立测试集、污染检测、监督 token 配比、自动补齐生成或目标模型训练。
