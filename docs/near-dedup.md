---
title: 跨版本近似去重
nav_order: 10.6
---

# 跨版本近似去重

本地 SQLite 持久索引帮助比较历史与新增 CPT/SFT 数据。使用确定性的字符 MinHash/LSH 召回候选，
再计算哈希字符片段集合的 Jaccard 相似度；默认阈值 0.9。这是词面近似检测，不判断事实等价或语义重复。
不调用模型、下载权重，也不改原始发布包。可参考 [DataTrove 的 MinHash 分阶段流程](https://github.com/huggingface/datatrove/blob/main/examples/minhash_deduplication.py)；
本项目是独立的轻量实现，不使用其分布式执行器或声称与其指纹兼容。

## 1. 按版本追加索引

```sh
python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json index --inputs data/training/snapshots/cpt-v1

python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json index --inputs data/training/snapshots/cpt-v2
```

输入为非空 CPT v2 或 SFT v2 包，先校验完整清单与血缘，单次输入不能混用 CPT 与 SFT。
建议使用累计 CPT 快照；引用旧样本的增量必须同时提供其依赖包。单次输入池沿用一百万条上限，索引可以分批追加。
同一内容重复导入不重新计算 MinHash，但追加新的来源引用；一批失败整体回滚。
数据库必须使用专用文件，不能指向 pipeline.db 或放入不可变数据包内。
配置和算法版本绑定索引，修改阈值、分片或长度限制后使用新的索引路径。

## 2. 冻结本次输入池的去重报告

```sh
python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json scan \
  --inputs data/training/snapshots/cpt-v1 data/training/snapshots/cpt-v2 \
  --output data/reports/near-cpt-v2
```

扫描只读索引，所有当前输入须先入库；输出目录必须是新目录且位于输入包之外。
报告包括：

- `decisions.jsonl`：每条当前样本的保留/排除决定、内容哈希、保留对象与原因。
- `relations.jsonl`：相似样本对、Jaccard 值、是否仅存在于历史索引及匹配来源。
- `origins.jsonl`：当前样本的原始来源引用，包括被排除的样本。
- `manifest.json` 与 `checksums.sha256`：输入包身份、索引包集合、算法参数、统计与完整性清单。

按首次入库顺序保留当前池中最早的直接匹配样本，同一批按稳定内容 ID 排序。
不会沿 A≈B、B≈C 就直接认定 A≈C。只在同一 split 内比较；validation 全量保留。
历史匹配若不在本次输入池，仅记关系，不排除当前样本。需要跨版本实际过滤时，把相应历史包纳入 scan 和后续配比的 inputs。
原样本的来源不会伪装成保留对象的精确来源；近似关联独立保存在报告中。

## 3. 接入配比构建

在现有配比 JSON 中增加：

```json
{"near_dedup_report": "data/reports/near-cpt-v2"}
```

`inputs` 必须与报告的输入包完全一致，内容变化必须重新扫描。
运行现有 `training.mixture_cli plan/build/verify` 即可。先排除训练池近似副本，再计算配额和筛选；
validation 保留。配比包携带 `near-dedup/` 冻结报告，后续不依赖 SQLite 即可校验与回查。
配额仍可能不足，沿用 `allow_shortfall` 和 `redistribute`；保留对象进入候选池后仍受后续配额/质量筛选约束。

首版支持固定与温度配比。DoReMi/RegMix 权重绑定原实验池，暂不允许直接叠加去重过滤，避免把旧权重当成新池的学习结果。
可通过同一 `.env` 启用[每日训练版本发布](training-publication.md)，自动维护专用索引并将冻结决策接入配比。独立 CLI 仍支持上述操作；未配置报告时保持原配比行为。

## 保守规则与边界

- NFKC、大小写和空白归一化后比较。短于 `min_chars`（默认80字符）仅检测归一化后完全相同的文本。
- 数字序列及内置单位序列不同不自动合并；这是保守保护，不代表金融事实一致性校验。中文数词及未列举单位仍需业务规则补充。
- CPT 按 tokenizer 身份隔离。SFT 按任务、system、证据上下文及原图哈希隔离，再比较问答文本，避免相同材料掩盖不同任务。
- 当前 SFT 使用 FinFlow 的材料/任务格式拆分上下文；多模态只对相同原图下的文本去重，不做图片感知哈希。
- LSH 是概率召回，可能漏掉近似对；相似度基于哈希片段集合，不能宣称全量语义去重。
- 超过 `max_candidates` 或 `max_chars` 时退出2，不截断后发布“成功”结果。扫描当前池和关系保存在内存，超大语料需要分批或扩展流式执行。
- 不增加独立测试集或污染检查；下游可根据业务自行扩展。
