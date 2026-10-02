---
title: SFT 数据生成
nav_order: 5.2
---

# 基于证据生成 SFT 数据

FinFlow 可从本机已校验的 v7 证据发布包生成三类中文文本 SFT：

| 配方 | 输入 | 产物与检查 |
| --- | --- | --- |
| `document_qa` | 审核通过的原文片段 | 问题、答案、精确引文；独立审核事实与可回答性 |
| `extraction` | 审核通过的原文片段 | JSON 字段抽取；字段值必须直接出现在引用证据中 |
| `table_calculation` | 已通过图表候选审核的表格 OCR、上下文与原图 | 两个操作数的计算；程序重算，审核模型对照原图核对取数与单位 |

当前是第一阶段的**文本 SFT**：训练输入包含材料和问题，不包含图片；表格原图仅用于审核。
不生成多模态训练样本、偏好对、检索负例或多轮对话，也不启动模型训练。
不把图表模型描述、翻译或改写当作新的事实依据。

## 1. 配置模型

使用现有项目环境，无新增依赖。凭证只填本机 `.env`：

```dotenv
FIN_DOC_SFT_GENERATE_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_GENERATE_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_GENERATE_MODEL=YOUR_GENERATION_MODEL
FIN_DOC_SFT_REVIEW_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_REVIEW_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_REVIEW_MODEL=YOUR_REVIEW_MODEL
```

两者均为兼容 Chat Completions 的接口；生成与审核是分开的调用。
启用 `table_calculation` 时，审核模型需要支持图片输入。
可选 `*_MODEL_VERSION`、`*_TIMEOUT`、`*_MAX_TOKENS` 见 `.env.example`。
不会自动借用已有 CPT 模型凭证。

复制配方并做本地检查：

```sh
cp -n config/sft.json config/sft.local.json
python -m training.sft_cli --config config/sft.local.json preflight
```

预检不请求模型，也不验证服务连通性或实际视觉能力。
独立 SFT 入口不要求重新配置 OCR 或 CPT tokenizer，但需要本机原数据目录、数据库和 v7 发布包。

## 2. 生成一个数据集

从每日飞轮 JSON 摘要的 `release.release_id` 获取发布包 ID。
当前 `config/flywheel.json` 中相应来源的 `source_policy.<source_name>.training` 必须仍为 `approved`。
如果使用本机飞轮配方，通过全局参数 `--flywheel-config config/flywheel.local.json` 指定。

```sh
python -m training.sft_cli \
  --config config/sft.local.json \
  --flywheel-config config/flywheel.local.json \
  --data-root data \
  build --dataset-id sft-001 --release YOUR_V7_RELEASE_ID
```

所有全局参数放在 `build` 前。只有 v7 发布包内、且当前数据库仍批准的候选可作为输入；
候选审核发生变化时，需要先发布新的 v7 包。独立复制来的包若没有本机对应的血缘数据库，不能直接生成。

生成流程：

1. 校验发布文件、候选和证据的 checksum，复查来源准入及最新候选决定。
2. 固定同源文档和完全相同上下文的训练／验证划分。
3. 按任务配方生成候选；校验 JSON、精确引用、抽取值和计算表达式。
4. 独立模型检查事实、问题可回答性、单位、日期与指令遵循；表格任务附原图。
5. 仅导出 `accepted` 样本，记录拒绝、待复核、跳过与失败原因。

短文本可能没有足够信息，允许生成零个样本。超出字符上限的材料会排除并记录原因，
不会截断表格或将一条问答拆成多个训练样本。

## 3. 配方与计算范围

`config/sft.json` 默认启用三种任务，每个证据／任务最多生成两条样本：

- `max_jobs`：每次最多完成的新生成任务数，已完成缓存不占名额。
- `max_model_requests`：单次 SFT 调用次数上限；命中缓存不计数，与 CPT 限额独立。
- `max_seconds`：开始下一次模型请求前检查时间；正在进行的请求可延续至该角色的 timeout。
- `max_evidence_chars`、`max_question_chars`、`max_answer_chars`：字符上限。
- `validation_fraction`：首次分配比例；已冻结或已由 CPT 分配的文档保持原划分。
- `generation_prompt_version`、`review_prompt_version`：提示词修订标识。

这些是工作量和长度限制，不包含供应商价格表或货币预算。
导出保持完整 `messages`，目标训练程序负责 tokenizer、chat template、长度检查和 packing。

表格计算首版只支持同单位的两个操作数：

| 运算 | 公式 |
| --- | --- |
| `sum` | a + b |
| `difference` | a − b |
| `ratio` | a / b |
| `percentage` | a / b × 100% |
| `growth_rate` | (a − b) / b × 100%，要求 b > 0 |

金额数值必须能在精确引文中找到，单位也须有原文引文。程序使用 Decimal 和四舍五入，支持 0–6 位小数，
不执行模型生成的代码、不做单位换算、不处理百分数操作数。行列含义、报告期与口径仍依赖审核，
程序算对不代表取数正确。

## 4. 输出、审核与血缘

输出目录：`data/training/sft/datasets/<dataset-id>/`。

| 文件 | 内容 |
| --- | --- |
| `train.jsonl`、`validation.jsonl` | `sample_id` 和 `messages`，可交给支持对话格式的训练程序 |
| `samples.jsonl` | 完整样本：任务、划分、生成／审核工件、引文、计算依据 |
| `evidence.jsonl` | 使用的材料、来源、work ID、chunk／表格位置、上游决定 |
| `audit.jsonl` | 审核结果、确定性校验失败、排除与重复记录 |
| `jobs.jsonl` | 可追溯的生成任务与本机 checkpoint 索引 |
| `manifest.json`、`checksums.sha256` | 配方、模型身份、输入发布包、数量、状态、完整性摘要 |

训练文件中每条样本为三条消息：`system`、含材料和任务的 `user`、`assistant`。
`sample_id` 是元数据，不应拼到提示词中。数据是合成样本，自动审核不等于人工验证；上线训练前仍需抽查。

```sh
python -m training.sft_cli verify data/training/sft/datasets/sft-001
python -m workflow.flywheel_cli --data-root data trace --sample-id YOUR_SFT_SAMPLE_ID
```

`trace` 可追到生成／审核响应、上游候选、原文或表格及 OCR/PDF 血缘。
训练文件包含材料，可单独用于文本训练；完整审计引用原数据目录，归档时应同时保留该目录和发布包。

## 5. 断点续作与数据划分

响应和已完成任务按输入内容、模型身份与配方缓存。网络失败、请求数或时间达到上限时，
导出已有审核通过的数据，状态为 `partial`；退出码为 1，配置或致命错误为 2。
使用**新 dataset ID、相同配方和发布包**继续，会复用已有结果并处理剩余任务。

同一个 dataset ID 已存在时，只校验并返回历史快照，不向其中追加。
每个新导出是选定发布包与配方的累计结果，不要把多次续作快照直接拼接，否则会重复训练。
拒绝／待复核结果不会自动反复生成；调整提示词版本或模型后生成新配方，保留旧审计记录。
当前没有 SFT 人工批准命令，上游候选人工复核也不能直接批准 SFT 样本。

划分在生成前持久化：沿用已有 CPT 的 work 划分；完全相同上下文与同一 work 的派生内容保持一致。
新的 CPT 构建也会读取该划分。历史数据冲突时排除相关 SFT 证据，记录 `split_conflict`。
本版只有 train/validation，不创建独立测试集；验证集可能参与配方调优，不能当作最终盲测。
该策略不自动识别所有语义近重复、跨公司转载或未建立身份关联的修订文档。

## 6. 每日自动生成

在本机飞轮配置中启用：

```json
"sft": {"enabled": true, "config": "config/sft.local.json"}
```

相对路径以飞轮配置所在目录的上一级为基准，通常是项目根目录。
执行原来的每日命令即可：

```sh
python -m workflow.flywheel_cli --config config/flywheel.local.json run
```

SFT 在证据发布与 CPT 构建后执行，使用剩余飞轮时间和独立的 SFT 请求上限。
没有待生成工作时复用缓存；每日摘要和聊天渠道日报增加 `sft_dataset`。
SFT 失败会令本轮状态为 `partial`，已发布的 CPT 数据保留。
默认 `enabled=false`，添加该功能不会自动安装调度或调用模型。

## 实现状态

实现了第一阶段三类文本 SFT、缓存续作、程序计算、独立审核、共享划分和工件血缘。
静态检查与文档构建检查不代表功能测试或真实模型验收；当前新增 SFT 尚未进行这些验收。
