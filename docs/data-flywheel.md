---
title: 每日数据飞轮
nav_order: 3
---

# 每日数据飞轮运行说明
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

实现入口：`workflow.flywheel_cli`。流程是来源发现 → 持久队列 → 下载与 PDF 校验 → OCR → 图表提取 → 文本治理 → 模型候选与独立审核 → v7 证据发布 → 文本 CPT 数据增量。

当前实现生成预训练数据文件，不启动 GPU 训练。已有 v5/v6 发布与预训练命令继续保留；v7 使用本页的飞轮入口，避免绕过 tokenizer、来源准入和候选审核。

## 1. 环境和模型配置

建议在项目独立虚拟环境运行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

激活环境后，以下命令使用该环境的 `python`。启动脚本依次选择显式 `PYTHON_BIN`、`.venv/bin/python`、兼容的 `.venv-flywheel/bin/python`、系统 `python3`。

将实际模型连接填写到项目 `.env`。已经有 `.env` 时只补字段，不用示例覆盖现有凭据。

| 角色 | 必需配置 | 何时启用 |
| --- | --- | --- |
| 视觉描述 | `FIN_DOC_VISION_API_URL/API_KEY/MODEL` | `methods` 包含 `visual` |
| 独立评审 | `FIN_DOC_PRETRAIN_REVIEW_API_URL/API_KEY/MODEL` | 正式飞轮必需；视觉候选评审模型也需支持图像 |
| 翻译 | `FIN_DOC_PRETRAIN_TRANSLATE_API_URL/API_KEY/MODEL` | `methods` 包含 `translate` |
| 改写 | `FIN_DOC_PRETRAIN_REWRITE_API_URL/API_KEY/MODEL` | `methods` 包含 `rewrite` |
| 文本元数据治理 | 现有 `LLM_API_URL/API_KEY/MODEL` | `governance_backend=auto` 有 key 时启用；`none` 禁用 |

各角色支持 `_MODEL_VERSION`、`_TIMEOUT`、`_MAX_TOKENS`。供应商升级同名模型时，应修改版本字段，使旧结果保留并生成新版本。endpoint 填 OpenAI 兼容的 base URL，不填带凭据或 query 参数的地址。API key 只从环境读取，不保存到处理记录。

同时配置：

```dotenv
FIN_DOC_FLYWHEEL_OCR_BACKEND=local
FIN_DOC_PRETRAIN_TOKENIZER=/absolute/path/to/target-model/tokenizer.json
FIN_DOC_PRETRAIN_MAX_TOKENS=2048
FIN_DOC_FLYWHEEL_MAX_TASKS=100
FIN_DOC_FLYWHEEL_MAX_MODEL_REQUESTS=50
FIN_DOC_FLYWHEEL_MAX_SECONDS=7200
```

本地 OCR 使用现有 `PADDLEOCR_LOCAL_API_URL`；云端使用现有 cloud 配置。`config/flywheel.json` 默认关闭页级方向矫正和去畸变，保留原页面坐标，以便从 PDF 裁出表格图像，表格识别仍然开启。`FIN_DOC_FLYWHEEL_OCR_OPTIONS` 可以覆盖这组请求选项。旧 OCR 按保存的实际参数处理，参数未知或坐标变换不可核实时保留待复核记录，不推测裁剪区域。

OCR 返回的区域图片优先使用。`outputImages` 属于页面或可视化用途，不能直接当正文图表。[PaddleOCR 官方结果字段说明](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html)是字段区分的依据。

预检：

```sh
python -m workflow.flywheel_cli preflight
```

预检检查依赖、启用模型的配置、OCR 连接配置和 tokenizer 文件，不请求网络服务。第一次实际调用仍可能出现供应商鉴权、上下文长度、图像支持或响应格式错误，这些会进入任务台账。

## 2. 来源和准入策略

来源目标继续放在 `config/collection.json`。生成配方、质量策略和预算放在 `config/flywheel.json`。学术发现默认分别扫描今日近期更新窗口、一个未完成的旧近期窗口和历史窗口，独立保存每个窗口的参数与 cursor；历史积压不阻断每日新增记录发现。近期窗口重叠两天并用稳定 ID 去重。返回结果集会随来源更新变化，首次真实试跑仍需抽查来源覆盖；[Crossref 分页更新说明](https://community.crossref.org/t/changes-to-cursors-filtering-and-sorting-in-the-rest-api/16246)说明了这一限制。默认方法是 `original` 和 `visual`；要增加翻译、改写，可设置：

```json
"methods": ["original", "visual", "translate", "rewrite"]
```

分页请求保持每个窗口的 query、过滤器和页大小不变，完整登记每页记录后才保存下一 cursor。发现预算按完整页计算，`max_results` 非页大小整数倍时可能多返回不足一页的记录，后续处理仍受任务预算限制。OpenAlex 页大小上限为 100；如需增加 API 配额，可在 `.env` 配置 `OPENALEX_API_KEY`，通过 Authorization header 发送，不拼入持久化 URL。参见 [OpenAlex 官方鉴权说明](https://help.openalex.org/api/authentication/)。

`source_policy` 默认空，代表尚未确认训练使用。先确认拟使用范围，再为相应来源填写，例如：

```json
"source_policy": {
  "cninfo": {"training": "approved", "usage_scope": "internal-only"}
}
```

这项批准必须反映实际来源使用决定，不能把开放获取或抓取成功当成自动批准。Crossref、OpenAlex 发现的文章需按其实际 PDF 许可检查；如果同一发现来源内许可不同，先保持来源未批准，针对具体候选完成人工确认后再决定更细的来源策略。本版策略以来源名为粒度，不自动判断每篇论文的训练授权。

默认 `require_ocr_pass=true`，OCR 有告警的候选先留待复核。原文、视觉描述、翻译、改写分别判定，技术失败进入有上限的重试；拒绝、截断、冲突和业务待复核不会自动反复请求。正式路径没有 mock 回退。

建议首次少量运行并按四类候选抽查。视觉审核使用实际图片和显式记录的 OCR 上下文；数字差异是异常提示，由审核模型判断是否有来源支持，不作为唯一准入依据。

## 3. 日常操作

```sh
# 前台完整运行，便于首次观察
python -m workflow.flywheel_cli run

# 不发现新来源，补齐本地积压并回填已有 OCR
python -m workflow.flywheel_cli run --no-discover

# 状态与抽样
python -m workflow.flywheel_cli status
python -m workflow.flywheel_cli audit --status needs_review --limit 20
python -m workflow.flywheel_cli audit --method visual --limit 10

# 显式人工判定，保留判定人、原因、输入 hash 和之前的决定
python -m workflow.flywheel_cli review-candidate \
  --candidate-id CANDIDATE_ID --decision accepted --reviewer REVIEWER --reason '已核对原图和来源'

# 修复配置/服务后显式重试，保留旧尝试，新任务有新的身份
python -m workflow.flywheel_cli retry --task-id TASK_ID

# 回查完整多父级血缘
python -m workflow.flywheel_cli trace --sample-id SAMPLE_ID
python -m workflow.flywheel_cli trace --candidate-id CANDIDATE_ID
```

人工接受仍要求相应来源已批准训练使用。图片定位失败的记录保留原始 OCR、页码、bbox 和原因；应校正服务返回/坐标配置后重新处理该文档，不能用人工接受文字候选替代图像定位。

任务状态为 `pending/running/succeeded/retry_wait/failed/deferred/rejected/needs_review`；显式人工替换的旧任务为 `superseded`。租约和系统文件锁防止重复执行；持有每日整轮锁的新进程可以恢复已中断的旧任务。单项失败允许独立候选继续推进。

退出码：`0` 成功或无变化，`1` 部分完成/有积压，`2` 配置或整轮失败。每日摘要包含阶段错误类型、队列、视觉状态、候选状态、模型请求/用量、耗时、release 和数据集路径。请求预算跨治理、视觉、翻译、改写、评审共享；用量未返回时记录缺失。

任务限额和整轮时限是协作式边界：正在执行的 HTTP/OCR 请求由其自身 timeout 控制，整轮不会在请求中途强制杀进程。模型结果逐次原子保存；收到成功响应但还未完成下游任务时中断，下次复用该响应。网络响应到达但尚未保存时发生进程终止，供应商可能已计费，下次仍需请求。

## 4. 本地数据和数据库

所有输出在统一 `data_root` 下；默认 `data`，可用 `FIN_DOC_DATA_ROOT` 或 CLI 全局 `--data-root` 调整。飞轮状态数据库固定为该根目录的 `state/pipeline.db`，不继承指向其他数据根目录的旧 `FIN_DOC_STATE_DB`。

| 路径/表 | 内容 |
| --- | --- |
| `processing_task` | 持久任务、依赖、重试、租约、执行结果 |
| `visual_asset` | 图和表共用表，保留类型、来源、页码、bbox、图像 hash/路径与元数据 |
| `visual_description` | 每个图表的模型描述版本 |
| `artifact_edge` | 多输入依赖图；拒绝形成环 |
| `training_candidate` | 候选方法、内容身份、准入状态与判定 |
| `training_sample/training_origin` | 跨日内容去重、全部来源与实际 split |
| `media/blobs` | 按图片字节 hash 共用文件；不同出现位置各自保留记录 |
| `media/tables` | 表格原 OCR HTML/内容和治理 Markdown 表示 |
| `media/contexts` | 实际关联的 OCR 上下文证据 |
| `media/unresolved` | 无法安全定位的图表记录 |
| `processed/governed_versions` | 按 OCR/规则/模型身份保存正文治理版本 |
| `processed/visual_descriptions`、`processed/transforms` | 原始模型响应、实际提示词、输入身份 |
| `training/candidates`、`training/decisions` | 候选与模型/人工质量判定文件 |
| `manifests/daily` | 每日摘要 |

首次在已有数据库增加飞轮表前，通过 SQLite backup API 保存 `pipeline.db.before-flywheel.bak`。迁移为增量建表，不删除旧表。第一次 `status` 等需要数据库的操作也会触发迁移；`preflight` 不迁移数据库。回填已有 OCR 不再次调用 OCR 服务。

恢复数据库前先停调度，并同时保存数据库、WAL 和整个数据根目录的当前副本；不要只移动正在使用的 `.db` 文件。旧备份恢复后，新增队列和人工判定可能需要从新备份恢复，不能假设仅靠语料文件重建全部状态。

## 5. 发布与训练数据

治理包位于 `published/flywheel/RELEASE_ID`。v7 包含正文记录、图表清单、描述、候选、决定、artifact DAG 与实际证据文件。`artifacts.jsonl` 把运行目录中的来源路径映射到包内 `evidence` 路径；所有文件都有 SHA-256 校验。校验和验证完整性，不代表数字签名或来源认证。

正式语料只从校验通过的 v7 冻结候选读取。每日训练增量位于 `training/pretrain/DELTA_ID`，包括 `corpus.jsonl`、`provenance.jsonl`、`excluded.jsonl`、`manifest.json` 和 `checksums.sha256`。token 上限按目标 tokenizer 对最终正文计数，不包括训练程序未来自行添加的 special tokens；训练程序需自行预留相应空间。样本格式包括 `sample_id/text/split/token_count`。

没有新样本时不生成空训练数据集。若只有新血缘，则在 `training/origin_deltas` 保存血缘增量。完全没有变化则记录 `no_change`。发布目录原子落盘后 SQLite 索引尚未提交时，下一轮验证文件并恢复索引。

同一逻辑作品的原文、翻译、改写和视觉描述共用划分。重复内容连接的作品也沿用同一组；新增连接若同时碰到已经发布的 train 和 validation，相关候选先隔离为 split conflict，保持已发布数据不变，等待重新选择/构建划分。去重和分组按 tokenizer、token 上限、划分比例及脱敏版本形成的 recipe 隔离；切换目标 tokenizer 不会被旧语料索引误判为已经导出。当前内容版本策略是追加不同的合格内容，不自动删除旧模型版本。

累计快照显式选择增量：

```sh
python -m workflow.flywheel_cli snapshot \
  --dataset-id finance-cpt-v1 --delta DELTA_1 --delta DELTA_2 \
  --origin-delta OPTIONAL_LINEAGE_DELTA

python -m workflow.flywheel_cli verify data/training/snapshots/finance-cpt-v1
```

选定增量若引用更早样本，需包含其原始增量。不同 tokenizer 配方不能混合。近似重复报告采用有界字符 n-gram Jaccard，仅报告，不自动删除；报告记录采样长度、比较上限和是否检查完整。

## 6. 每日定时

`scripts/run_collection.sh` 已转为飞轮的兼容启动入口；`scripts/run_flywheel.sh` 安全加载 Python 环境并把输出追加到 `logs/flywheel.log`。旧的独立采集 CLI 仍可手动使用，但不要同时给旧流程和飞轮安装定时任务。

配置、预检和少量前台实跑成功后再启用调度。当前没有安装或启用系统任务。

Linux/macOS cron 示例是 `ops/fin-doc-flywheel.cron.example`，默认主机当地时间每天 03:00。模板路径需改为实际仓库绝对路径；核对主机系统时区，例如 Asia/Shanghai。日志目录由脚本创建。

macOS launchd 模板是 `ops/com.chansn.fin-doc-flywheel.plist.example`，每天 03:00，登录加载时也运行一次，便于补齐积压。先修改模板中的仓库路径，再启用：

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp ops/com.chansn.fin-doc-flywheel.plist.example "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
```

停用：

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
```

日历触发使用 macOS 系统时区。电脑关机或 OCR 服务不可达时无法保证实时完成；恢复后通过队列继续处理。launchd 休眠补触发的行为以系统运行情况为准。

## 7. 当前验证边界

自动化测试使用临时数据根目录、合成 PDF/图像、测试 tokenizer 和注入的假模型响应；假模型不会从正式 CLI 配置进入发布。已覆盖重跑、租约、互斥、血缘环、token 边界、图表定位、审核阻断、故障重试、模型结果缓存、跨日去重和发布校验。

真实来源服务、OCR 返回差异、多模态描述质量、实际模型鉴权、供应商用量、首次分层人工抽样和系统调度实跑仍需配置后验证。日志目前为本地追加文件，长期运行需由系统日志轮转控制大小。大规模证据快照与候选扫描目前使用累计视图，后续数据量增长时应增加分批索引和存储保留策略。
