---
title: 数据质量与覆盖报告
nav_order: 10.5
---

# 数据质量与覆盖报告

报告读取已发布且校验和完整的 CPT v2 / SFT v2 数据包，输出 JSON 和 Markdown。
无需模型凭据，不发起模型调用。CPT 血缘增量也可读取，但其中可能没有训练正文。

```sh
python -m training.quality_report \
  --input data/training/snapshots/cpt-all \
  --output data/reports/cpt-all

python -m training.quality_report \
  --input data/training/snapshots/cpt-next \
  --baseline data/reports/cpt-all \
  --targets config/coverage-targets.json \
  --output data/reports/cpt-next
```

输出目录必须尚不存在，且不能位于输入数据包内。报告目录包含 `report.json`、`report.md`、
`manifest.json` 和 `checksums.sha256`。输入校验失败时拒绝生成报告；CLI 失败退出码为 2。

## 统计与解释

- 样本数、唯一内容数、包内精确重复副本及比例、逻辑文档数、图片数；CPT 统计包内保存的目标 tokenizer token 数。
- 按 split、来源、来源语言标签、生成方法、任务、策略、模态统计覆盖；未知标签记为 `unknown`。
- 同一文本有多个来源时，每个来源计一次，因此来源桶之和可以超过样本或 token 总量。
- SFT 不估算监督 token；需在后续训练 tokenizer、聊天模板和 loss mask 固定后统计。
- 报告中的语言来自来源元数据，不能据此断言翻译后正文的语言。
- 审计条目统计状态与 reason 字段，不输出自由文本审核解释。审计条目可能重复且不包含统一候选分母，因此通过率为 `null`。
- 包内检查相同内容、逻辑 work、图片跨 split；不声称完成语义去重、独立测试集污染检查或事实正确性评测。
- 报告保留内容哈希用于版本比较，不复制样本正文或模型响应。

## 版本对比与覆盖缺口

`--baseline` 接受之前生成并通过校验的报告目录。只有类型、统计范围、tokenizer 身份相同才能比较；
禁止把当天增量当作累计快照比较。记录新增、移除、保留内容数，划分变更，指标和覆盖桶差值。
配方变化会单独标记；token 差值不等于模型质量提升。

`--targets` 接受 JSON 数组，配置每个维度标签的最低样本数。样例仅演示配置，不代表推荐训练比例：

```json
[{"field":"task","label":"table_calculation","min_samples":100}]
```

支持 `source_name`、`language`、`method`、`task`、`strategy`、`modality`。
输出 `actual_samples` 和 `missing_samples`，当前仅报告缺口，不自动安排模型生成。

## 每日运行

每日飞轮自动为本轮产出的 CPT 和可选 SFT 包生成报告，目录为
`data/reports/training/<run_id>/cpt/` 和 `sft/`。日报 JSON 的 `quality_reports` 给出路径和关键指标。
未产生数据包时记为 `not_available`；报告失败会使运行标为 `partial` 并记录错误类型，已发布数据保留。
每日 CPT 通常是增量，SFT 是当前选定证据/配方的快照，两者不能直接累加成“今日新增样本”。
报告本身成功并不代表样本通过事实验收；请同时检查 `warnings`。
