---
title: 数据配比与选择
nav_order: 5.5
---

# 数据配比与选择

`training.mixture_cli` 消费不可变 CPT v2 或 SFT v2 数据包，保留来源血缘并导出新的训练配方。
它不调用生成模型。CPT 和 SFT 分别构建；验证集完整保留，不参与训练配额或筛选。

## 先预览，再发布

```bash
cp config/mixture-cpt.json config/mixture-cpt.local.json
# 编辑 inputs、分组、预算等；所有相对路径相对于命令执行目录（项目根目录）。
python -m training.mixture_cli --config config/mixture-cpt.local.json plan
python -m training.mixture_cli --config config/mixture-cpt.local.json \
  build --output data/training/mixtures/cpt-001
python -m training.mixture_cli verify data/training/mixtures/cpt-001
```

CPT 推荐先用已有 `workflow.flywheel_cli snapshot` 构造完整累计快照。
如果输入增量引用了未选入的历史样本，配比会拒绝缺失的血缘。
SFT 使用 `config/mixture-sft.json`，可合并文本/视觉包；重复的累计快照会按内容去重并保留来源，
发现相同内容、图片或已知文档家族跨 split 时拒绝处理。

## 方法与参数

| 参数 | 含义 |
| --- | --- |
| `group_fields` | `method`、`task`、`strategy`、`source_name`、`language`、`modality` 中的一个或多个字段 |
| `method` | `fixed`、`temperature`、`doremi` 或 `regmix` |
| `unit` | CPT：`tokens` 或 `samples`；SFT：`samples` |
| `budget` | 训练集总配额，正整数 |
| `max_per_family` | 可选；同一已知文档家族最多入选多少条，包括翻译/改写后代 |
| `seed` | 决定组次序及组内稳定随机排序 |
| `redistribute` | 是否把未填满的配额分给仍有容量且权重大于零的组，默认 false |
| `allow_shortfall` | 是否允许发布不足额包，默认 false |

多字段标签按 `|` 连接，例如 `document_qa|answer_first`。缺失字段为 `unknown`；
相同内容的多个来源在某个字段上不同则为 `mixed`，不会重复计数。现有来源未必提供语言标签，
此处不会自动猜测语言。分组表示互斥组合，不支持同时求解任意多组交叉边际约束。

固定配比示例：

```json
{"method": "fixed", "group_fields": ["method"], "weights": {"original": 0.7, "translate": 0.2, "distill": 0.1}}
```

这是说明格式的示例比例。`weights` 必须覆盖实际训练池的全部组，可以显式填 0；先运行温度模式的 plan 查看组名。

温度模式采用 `p_i = n_i^alpha / sum(n_j^alpha)`：`alpha=1` 保持规模比例，
`alpha=0.5` 提升小组占比，`alpha=0` 对非空组均匀分配。参考 [XLM 实现](https://github.com/facebookresearch/XLM/blob/main/xlm/utils.py)。
这里的 n 根据 `unit` 使用 token 数或样本数，不代表质量评分。

CPT 的 token 数沿用已冻结的目标 tokenizer，token 配比要求全部输入使用相同 tokenizer 配方。
没有截断样本来凑配额：预算、完整样本长度、家族上限和多样性筛选可能导致短缺。
`plan` 输出每组可用量、目标量、入选量、实际占比和总短缺；不足额返回码为 1。
`build` 默认拒绝不足额，显式允许后保存 partial 状态。整个流程无放回采样。

## 导出格式

- `manifest.json`：输入快照身份、配方、目标和实际比例、tokenizer、状态。
- `records.jsonl`：选择 ID、内容哈希、分组、原样本 payload、全部已知 origins 和家族。
- CPT：`train.jsonl`、`validation.jsonl`。
- SFT：另有 `train.vision.jsonl`、`validation.vision.jsonl`；原图随 `images/` 和 `images.json` 打包。
- `checksums.sha256`：完整文件清单与校验值。

配比包有独立的 `finflow-mixture-dataset-v1` 契约，不使用原始 SFT 包的 verifier。
视觉加载使用 `training.mixture.load_vision_records(path, split="train")`，返回 messages 和实际 PIL 图片。
`records.jsonl` 的 origins 可回查原始 sample/artifact ID；完整历史审计仍需原始数据根目录。
配比使用冻结快照中的准入结果，不查询实时审核撤回；源政策变化后应重新发布批准的数据快照。

## 质量与多样性选择

可增加 `selection`：

```json
{"selection": {"scores_file": "data/scores/quality.jsonl", "metric": "jaccard", "threshold": 0.9}}
```

评分 JSONL 每行包含 `id`（配比池内容 ID）、`content_hash`、`quality`、`complexity`（均为 0–5）。
使用 `cosine` 时还需 `embedding`，所有向量维度一致、有限且非零。
先生成一个不带 selection 的配比包，可从 `records.jsonl` 取得身份；评分文件须覆盖后续输入池的全部训练样本，
不能只覆盖一个有配额限制的子集。也可调用 `training.mixture_data.load_pool(inputs, group_fields)` 导出完整 ID 清单。

组内按 quality × complexity 排序，再过滤与已选样本相似度达到阈值的候选；全局家族上限仍生效。
Jaccard 使用全文三字符集合；SFT 比较 assistant 回答，避免共同证据正文主导相似度。
它是词面多样性规则；cosine 需要外部生成的向量与评分，不会自动下载 DEITA 模型。
本实现为 [DEITA-inspired](https://github.com/hkust-nlp/deita)，并非原评分模型复现。
筛选采用直接两两比较，适用于受限的本地候选池，大规模数据应先分批。

## 学习得到的配比

见 [DoReMi / RegMix 实验](mixture-experiments.md)。生成 `weights.json` 后复制
`config/mixture-learned.json`，设置对应 `method`、`weights_file` 和相同 inputs/group_fields。
只接受同一冻结输入池和分组的权重，按 token 配额导出。
代理训练可重复访问训练块，静态配比导出无放回；因此学到的权重仍可能受实际容量限制。
