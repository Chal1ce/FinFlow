---
title: 训练数据生成方法
nav_order: 5.4
---

# 训练数据生成方法

任务决定训练什么能力，策略决定怎样生成候选，配比决定哪些合格候选进入训练包。
原有配方仍默认使用 `direct`；新增方法需要在本机配方中启用。

## CPT 文档变换

在 `config/flywheel.local.json` 的 `methods` 中加入所需方法：

| 方法 | 产物 |
| --- | --- |
| `distill` | 保留限定条件的信息密集段落 |
| `textbook` | 对来源明确解释的概念、规则进行教材化组织 |
| `knowledge_list` | 保留主体、指标、数值、单位和期间的事实条目 |
| `diverse_qa` | 由当前文档支持的多角度阅读理解问答文本 |

这些方法共用 `.env` 中的 `FIN_DOC_PRETRAIN_SYNTHESIZE_API_URL`、`_API_KEY`、`_MODEL`；
独立审核继续使用 `FIN_DOC_PRETRAIN_REVIEW_*`。模型端点仍是 OpenAI-compatible base URL。
原有 `translate`、`rewrite` 保持各自角色。新方法与原文分别保存候选，不能从模型常识补充定义、事实或因果。

方法参考 [Nemotron-CC 官方任务](https://docs.nvidia.com/nemo/curator/curate-text/synthetic/nemotron-cc/tasks)，
以 FinFlow 的证据、审核和血缘接口重新实现，未引入 NeMo 运行时。
CPT 问答仍是普通文本，不自动成为通过 SFT 审核的 messages 样本。

## 多步骤文本 SFT

```bash
cp config/sft-methods.json config/sft-methods.local.json
python -m training.sft_cli --config config/sft-methods.local.json \
  --flywheel-config config/flywheel.local.json preflight
python -m training.sft_cli --config config/sft-methods.local.json \
  --flywheel-config config/flywheel.local.json \
  build --dataset-id sft-methods-001 --release YOUR_RELEASE_ID
```

`tasks` 与 `strategies` 分开配置。非 `direct` 策略当前只用于 `document_qa`；
抽取、程序计算和表格转录使用各自的 `direct` 契约。配置缺少兼容策略时预检失败。

| 策略 | 实际执行步骤 | 适配边界 |
| --- | --- | --- |
| `direct` | 生成候选 → 确定性检查 → 独立审核 | 原有基线 |
| `self_instruct` | 种子扩展 → 指令筛查 → 单独生成答案 → 审核 | 固定种子、单次证据内扩展；不自动维护跨文档自举池 |
| `answer_first` | 选择精确原文答案 → 反推问题 → 筛查 → 原文答案审核 | API 适配；没有训练反向模型或执行 Humpback 迭代微调 |
| `evol_instruct` | 基础题 → 多轮增加约束/限定范围/比较 → 信息增量与可回答性筛查 → 答案与审核 | 限于当前证据，不做开放主题扩张 |
| `codeclm` | 用途/技能/量表编码 → 指令解码 → 按量表改进 → 答案与审核 | 可选再启用目标模型对比；没有执行目标模型微调 |

来源：[Self-Instruct](https://github.com/yizhongw/self-instruct)、
[Evol-Instruct](https://arxiv.org/abs/2304.12244)、
[Instruction Backtranslation](https://arxiv.org/abs/2308.06259)、
[CodecLM](https://research.google/blog/codeclm-aligning-language-models-with-tailored-synthetic-data/)。

### 参数与模型

- `seed_instructions`：可选的 1–100 条金融任务种子；默认内置五类任务形态。不要使用验证集答案作为种子。
- `strategy_seed`：选择种子、演化操作的随机种子，默认 42。
- `evolution_rounds`：演化轮数 1–5，默认 2。
- `contrastive_filter`：默认 false；设 true 时必须启用 `codeclm` 并配置 `FIN_DOC_SFT_TARGET_*`。
- `contrastive_min_gap`：教师与目标回答的量表评分差阈值，整数 1–5，默认 1。

生成使用 `FIN_DOC_SFT_GENERATE_*`，指令筛查及最终审核使用 `FIN_DOC_SFT_REVIEW_*`。
启用 CodecLM 对比后，目标模型仅接收原始证据和问题；审核模型按量表比较两份回答，
教师回答仍须通过最终事实审核。评分差是合成数据选择信号，不是模型能力提升的证明。

每个策略任务保存中间响应、角色、stage 工件、最终样本和审核；样本包含 `strategy`、`strategy_stages`。
配方哈希包含种子/策略配置和模型身份；引擎升级为 `sft-v3`，导出数据契约仍为 SFT v2。
原有数据包保持不可变。多步骤任务需要更多模型请求，达到上限时换新的 dataset ID 续作，
相同配方的响应缓存可复用；累计恢复快照不要直接拼接。
随机种子固定流程选择，不能保证远端模型逐字确定性；缓存保存实际响应供重放。

## 多模态任务扩展

复制 `config/sft-vision-methods.json` 为本机配方，可启用：

- `visual_description`：描述可见结构、标签、单位和趋势，不估计模糊图表的精确数值。
- `visual_conversation`：同一张图的 2–`max_conversation_turns` 轮问答，默认最多 4 轮，范围 2–8。

继续使用 `FIN_DOC_SFT_VISION_GENERATE_*` 和 `FIN_DOC_SFT_VISION_REVIEW_*`。
审核逐轮对照原图，检查矛盾和图外推断；图片只放在首个 user 消息，后续轮次共享该图。
每轮有长度限制，总回答长度也受 `max_answer_chars` 限制。
原有视觉问答和表格转 JSON 可同时启用。类别设计参考 [LLaVA 官方数据说明](https://github.com/haotian-liu/LLaVA/blob/main/docs/Data.md)，
使用真实图片 API 的实现属于流程适配。

## 选择与发布

生成数量不代表最终训练占比。先审核、去重，再使用[数据配比](data-mixtures.md)进行选择。
[DoReMi / RegMix](mixture-experiments.md)提供真实本地代理训练和配比学习，独立于日常 SFT/API 调用。
每种方法的来源和适配边界记录在 `training/method_cards.py`，相关 SFT/实验 manifest 会保存方法卡。
