# 单卡 Qwen3.5 SFT

面向单张 4090D 的首轮配方：`Qwen/Qwen3.5-0.8B`、BF16 LoRA、rank 8、batch 1、梯度累积 8、长度 2048，默认训练 100 步。直接使用已有 SFT 导出，不重跑数据治理、OCR 或教师审核。

Qwen 官方提供 [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B)；ms-swift 的[模型支持表](https://swift.readthedocs.io/en/latest/Instruction/Supported-models-and-datasets.html)包含该型号，参数参考其 [Qwen3.5 微调说明](https://swift.readthedocs.io/en/latest/BestPractices/Qwen3_8-Best-Practice.html)。本配方尚未在用户 GPU 上实跑，不预设吞吐或训练收益。

## 启动

将当前代码与已有 SFT 数据集放到 GPU 机器，在具备 CUDA 版 PyTorch 的 Python 环境中，从 FinFlow 仓库执行：

```sh
python -m pip install -e .
python -m pip install -r agent/trainers/sft/requirements.txt
python -m agent.cli sft \
  --dataset /path/to/existing-sft-dataset \
  --output data/agent/checkpoints/qwen3.5-0.8b-sft-001
```

`--dataset` 是现有 `training.sft_cli build` 产出的数据集目录，包含 `train.jsonl` 和/或 `train.vision.jsonl`，以及视觉样本引用的 `images/`。它不是 OCR 输出目录，也不是 `import-assets` 的资产清单。

默认合并文本与视觉样本。可用 `--modality text` 或 `--modality vision` 选择已有样本；`--max-steps` 调整更新步数；`--max-samples` 限制输入条数；`--model` 可指定本地模型目录。已有验证集会直接复用，不重新划分。

## 格式对接和输出

入口将现有图片内容块转为 ms-swift 的 `<image>` 标记，并把原图片路径绑定为训练机上的绝对路径。训练只监督现有配方指定的回答；图像本身交给模型 processor，不使用文字描述替代图像。

- `finflow-inputs/`：本次训练的格式转换结果，源数据只读。
- `finflow-run.json`：启动命令、样本数、实际依赖版本和执行状态。
- `checkpoints/`：ms-swift 的训练日志与 LoRA checkpoint；可能包含其版本子目录。

复跑使用新输出目录；继续已有训练时，保持模型/数据/配方一致并加 `--resume-from-checkpoint` 指向实际 checkpoint 目录。若只想生成输入和命令，可以使用 `--prepare-only` 和单独的输出目录。

该入口完成普通文本/视觉 SFT。工具 episode、Agent SFT 轨迹构建及 Agentic RL 环境/奖励/训练适配继续开发。
