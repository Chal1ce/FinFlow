# 复用现有治理资产

Agent 与视觉 SFT 共用 `training/sft_evidence.py` 的 `EvidenceLoader`，直接读取已有 v7 证据包和当前候选决定，继承图片、页/block、来源与血缘。导入器沿用已有审核结论。

```sh
python -m agent.cli import-assets \
  --data-root data \
  --release <已有v7发布目录名> \
  --output data/agent/assets/batch-001
```

默认读取 `config/flywheel.json` 的来源策略和 `config/sft-vision.json` 的图片/文本大小限制，可用 `--flywheel-config` 和 `--config` 指定已有配方。导入不需要配置模型。

输出 `assets.jsonl`、`excluded.jsonl`、`manifest.json`、`checksums.sha256` 和按内容去重的 `media/*.png`：

- `assets.jsonl` 保留上游 candidate/decision/artifact、work、页/block 与已存在的 SFT work split，可直接供后续 Agent 工具加载。
- `excluded.jsonl` 复用现有 SFT loader 的排除原因，便于按实际问题修复。
- 图片复制为独立文件，原有资产和 SQLite 只读；既有 SFT 继续使用同一 loader 和 splitter。
- 暂无 SFT split 的资产仍可导入，在生成训练任务时接现有划分逻辑。

该命令直接承接现有治理结果，不调用 OCR、教师或审核模型，不创建新的人工审核队列。它是资产适配接口；问答、Agent episode 与模型训练由相应生成器使用这些资产构建。

[OCR inventory](inventory.md) 保留为旧产物的可选诊断工具，用于检查文件与 OCR 映射。
