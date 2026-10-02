---
title: 图表多模态 SFT
nav_order: 5.3
---

# 图表多模态 SFT

从已批准的图表候选和原始区域图片生成真正包含图片的训练样本。
原有[文本 SFT](sft.md)保持可用，多模态任务使用独立配方与模型角色。

| 任务 | 输入 | 输出 |
| --- | --- | --- |
| `visual_qa` | 图片或表格原图＋问题 | 由图中可见内容支持的答案 |
| `table_structure` | 完整、清晰的简单矩形表格图片＋固定抽取指令 | 含 `title`、`unit`、`columns`、`rows` 的 JSON |

生成和独立审核均附原图。训练消息不包含 OCR 文本或已生成的描述，
审核也不读取邻近 OCR 上下文，要求仅凭提供给训练模型的图片就能回答。
OCR 仅作为生成时的辅助提示，不能补充图片之外的信息。

## 配置与生成

复制多模态配方：

```sh
cp -n config/sft-vision.json config/sft-vision.local.json
```

在本机 `.env` 中填写下面两组配置；两个模型都必须支持图片输入：

```dotenv
FIN_DOC_SFT_VISION_GENERATE_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_VISION_GENERATE_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_VISION_GENERATE_MODEL=YOUR_VISION_GENERATOR
FIN_DOC_SFT_VISION_REVIEW_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_VISION_REVIEW_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_VISION_REVIEW_MODEL=YOUR_VISION_REVIEWER
```

只启用多模态任务时不要求 `FIN_DOC_SFT_GENERATE_*` 和 `FIN_DOC_SFT_REVIEW_*`。
如果在同一配方中加入文本任务，则也要配置相应文本角色。
本地预检只检查配置，不能证明远端模型实际支持图片。

```sh
python -m training.sft_cli --config config/sft-vision.local.json preflight
python -m training.sft_cli \
  --config config/sft-vision.local.json \
  --flywheel-config config/flywheel.local.json \
  --data-root data \
  build --dataset-id vision-sft-001 --release YOUR_V7_RELEASE_ID
```

输入仍需本机 v7 发布包与对应数据库；当前来源准入和上游候选审核必须通过。
飞轮需要先启用 `visual` 方法并完成图表审核，才能得到可用的视觉候选。
达到次数／时间限制后，使用新 dataset ID、相同配方和发布包续作。

每日自动运行只需把本机飞轮配置指向多模态配方：

```json
"sft": {"enabled": true, "config": "config/sft-vision.local.json"}
```

默认飞轮仍关闭 SFT。一个每日运行使用一个 SFT 配方，配方的 `tasks` 可以同时包含五种文本／多模态任务。

## 质量约束

- 每个视觉样本需附有证据区域，坐标为相对于当前裁剪图的 0–1000 整数；区域及观察记录用于审核，不放入训练输入。
- 审核必须确认事实、数值、单位、日期、图片内可回答性、区域定位；表格转 JSON 还必须确认完整抄录。
- 表格只处理简单矩形结构；合并单元格、复杂多层表头、图片被截断或文字不可辨认时应跳过或待复核。
- 单元格保留字符串，不擅自转换数值、填充或换算单位；只有可见空白单元格使用 null，不能用 null 代替无法辨认的内容。
- 配方默认最多 50 行、20 列、500 个数据单元格；超限拒绝，不截掉行列。
- 图片默认最多 10 MiB、4000 万像素，须是单帧 PNG，并校验原图哈希。不会自动降采样或修改原图。

对应参数：`max_table_rows`、`max_table_columns`、`max_table_cells`、`max_image_bytes`、`max_image_pixels`。
问题／答案字符上限与其他 SFT 任务相同。若大表无法在模型输出限制内完整返回，应排除或调整配方，而非拼接截断结果。
区域位置和视觉事实仍由模型审核，需要人工抽查，不能把自动通过等同于标注真值。
图片按原字节打包，不会自动对图片做隐私遮挡。

## 便携导出与加载

SFT v2 输出分别存放文本与多模态数据：

| 文件 | 内容 |
| --- | --- |
| `train.jsonl`、`validation.jsonl` | 纯文本训练样本；仅视觉配方时为空 |
| `train.vision.jsonl`、`validation.vision.jsonl` | 含图片引用和类型化消息的多模态样本 |
| `images/<sha256>.png` | 随数据集复制的原图，相同图像只存一份 |
| `images.jsonl` | 哈希、尺寸、文件大小及原图工件血缘 |
| `samples.jsonl` | 完整样本、任务、审核与证据区域 |

其余 evidence、audit、jobs、manifest 和 checksums 文件延续文本 SFT。
`manifest.json` 增加模态数量、图片数量与导出文件索引。
把整个数据集目录移动到另一台机器后，训练图片引用仍然有效；完整历史审计仍需原数据目录。

多模态消息示意（哈希和答案为格式示例）：

```json
{
  "sample_id": "sft-example",
  "images": ["images/IMAGE_SHA256.png"],
  "messages": [
    {"role": "system", "content": [{"type": "text", "text": "仅依据图片回答。"}]},
    {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "图表显示的报告期是什么？"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "图片中标出的报告期。"}]}
  ]
}
```

`images` 在磁盘 JSONL 中是数据集目录内的相对路径。实际交给训练框架时需加载图片。
仓库提供按条读取的 PIL 适配器，不依赖 transformers、datasets 或 TRL：

```python
from training.sft_export import load_vision_records

records = load_vision_records("data/training/sft/datasets/vision-sft-001", split="train")
for example in records:
    # example 包含 messages 和 images；images 中是真实 PIL.Image 对象。
    # 将 example 交给你选择的多模态训练程序。
    print(len(example["images"]))
```

消息结构参考 [TRL 视觉数据格式](https://huggingface.co/docs/trl/main/en/dataset_formats#vision-datasets)：
类型化 content 与单独 images。目标模型的 processor、chat template、图像 token 和批处理方式由下游训练程序配置。
这里不宣称已经与某个训练框架完成联调，也不会自动启动训练。

## 校验、划分和版本

```sh
python -m training.sft_cli verify data/training/sft/datasets/vision-sft-001
python -m workflow.flywheel_cli --data-root data trace --sample-id YOUR_SFT_SAMPLE_ID
```

`verify` 对 v2 除 checksum 外，还检查训练文件与完整样本的一致性、图片引用、尺寸与哈希、占位符数量、
图片是否跨训练／验证集。老的 v1 文本数据包仍能执行 checksum 校验。

划分继承逻辑文档和已有 CPT 约束，并增加相同图片字节的约束。不同来源、不同 OCR 上下文引用同一图像时，
仍归入同一划分；与历史划分冲突则排除。不同裁剪、缩放、重新编码的相似图片不属于字节级去重范围。
多模态样本身份包含图像哈希，避免把两张图上的相同问答误合并。

引擎更新为 `sft-v2`，会生成新的配方身份；继续生成时请使用新的 dataset ID。
旧数据和审计不改写；完全相同请求的模型响应仍可命中缓存。
新文本／多模态导出都是累计快照，不能直接拼接多次续作结果。

## 当前状态

代码已加入多模态生成、原图审核、区域记录、便携图片导出、PIL 加载和增强校验。
尚未运行功能测试、真实模型验收或下游视觉模型训练；不会创建真实数据或调用付费模型来替代这些待办。
