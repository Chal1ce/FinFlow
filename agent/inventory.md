# OCR 图片资产摸底

读取 OCR 产物，登记可解码图片、视觉块关联与缺失文件。已治理的 v7 资产使用[治理资产导入](assets.md)；`inventory` 用于排查历史 OCR 文件映射。

## 运行

在 FinFlow 仓库或已安装环境中执行。`--input` 可以是一个 OCR 输出目录、一个 `result.json` / `result.jsonl`，或多个输出的父目录：

```sh
python -m agent.cli inventory \
  --input data/parsed_md \
  --output data/agent/inventories/batch-001 \
  --review-limit 20
```

输出目录必须尚不存在，且位于各 OCR 源目录之外。批量发现会优先使用装配好的 `result.json`，跳过 `segments/` 及 `*_history/` 备份，避免将分段或旧尝试当成当前文档；显式提供历史输出目录或文件仍可审计该版本。

默认只写清单和报告。需要便携查看图片时加 `--copy-media`：对通过解码检查的图片复制原字节到报告的 `media/`，按 hash 去重，不改编码、不修改 OCR 源文件。它会复制返回的整页图，大批数据可能占用较多空间，起步可限制样本：

```sh
python -m agent.cli inventory \
  --input data/parsed_md \
  --output data/agent/inventories/sample-001 \
  --max-documents 3 \
  --max-pages 20 \
  --review-limit 20 \
  --copy-media
```

`max-pages` 限制每份文档扫描的页数；未登记文件扫描覆盖该文档的 images 目录。manifest 保存限制与扫描范围，occurrence 保存页/block 关联状态。

默认单份结果最多 256 MiB，单张图片最多 10 MiB、4000 万像素；可用 `--max-result-bytes`、`--max-image-bytes`、`--max-image-pixels` 调整。超过限制、缺失、损坏、动图及不支持格式分别留诊断，不向远程补取图片。

## 输出与可见性

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 版本、选项、实现代码 hash、Pillow 版本、统计、文件校验和与 inventory_id |
| `sources.jsonl` | 结果文件 hash、扫描页数、来源 PDF 声明 hash、分组线索、来源错误 |
| `media.jsonl` | 媒体身份、来源页/key、实际 hash/MIME/尺寸、完整性和可选包内路径 |
| `occurrences.jsonl` | image/chart/table/figure block、原 bbox、边界检查、精确引用关联或 unresolved |
| `unmapped-files.jsonl` | images 下尚未登记关联的文件；用途只是路径分类提示 |
| `review-queue.jsonl` | 按标签/关联状态分类轮取的待复核项，含可用原页/区域引用 |
| `report.md` | 人可读统计及下一步 |
| `media/` | 可选的已解码图片原字节，候选状态保存在资产记录中 |

报告中的源路径相对于调用者指定的输入根目录；不把原始 OCR 正文、图片 Base64、宿主绝对路径或 `.env` 写入记录。未复制媒体时，加载器仍需调用者提供原输入根目录。报告可含真实业务图片，应保存在本地被忽略的数据目录，不能混入代码提交。

## 关联与质量语义

1. 新版使用页面 `image_assets` 的精确 source_key/path/page/hash，检查本地包含性、声明 hash、格式、单帧、像素和实际解码。
2. 旧版若保留明确 markdown 图片 key，可查看对应页限定文件或本页内嵌图片。Base64 仅在内存解码；远程 URL 不请求。没有登记 hash 的本地文件标为 `unregistered`，不能称为可信恢复副本。
3. 对旧版仍保留的 `inputImage`，登记其为返回的原页候选并保留 source JSON hash；不由用途标签推断它与原 PDF 的方向/坐标等价。
4. 视觉块只有在内容引用到本页唯一 region source_key 时，才记为 `exact_reference`。多候选是 ambiguous；没有引用是 unresolved。文件名里的 bbox 不用于猜测关联。
5. bbox 的数值/边界检查与原页定位可信度分开；本阶段 `localization_status` 保持 unverified，不对旋转/展平后的坐标执行 PDF 裁剪。
6. 未关联的 markdown 区域文件可进入人工队列并被解码查看；记录仍是 unregistered，页码和 block 待人工核对。调试/页面文件不默认作为区域问答材料。

`available` 表示图片在限额内可解码。诊断记录使用 `training_eligible=false`、来源批准 `unknown`、split `unassigned`；sidecar 提供的 PDF hash 标记为 `declared_not_verified`。训练准入读取上游治理决定。

## 诊断结果的使用

`review-queue.jsonl` 按缺图、关联失败与取数问题索引诊断样本。

`import-assets` 与视觉 SFT 共用 v7 证据 loader。诊断报告独立保存在输出目录，上游 SQLite 与候选决定保持只读。

未治理的旧文件通过原有 OCR/治理入口接入。

## 报告校验

```sh
python -m agent.cli verify-inventory data/agent/inventories/batch-001
```

校验文件 hash/完整清单、内部来源与媒体引用、复制媒体与原 hash 的绑定、计数和 inventory_id。人工标签应单独保存，避免修改包内文件后破坏校验和。

退出码：inventory 完整执行为 0，部分文档读取失败为 1，输入/输出参数或文件操作失败为 2；完整执行可以包含未关联、缺图等数据质量问题，具体看诊断计数。verify-inventory 成功为 0，校验失败为 2。
