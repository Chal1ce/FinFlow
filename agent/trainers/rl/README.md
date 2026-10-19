# 单卡金融 Agentic RL

当前代码提供这条训练链路：

```text
版本化任务/表格/私有 oracle
    → Qwen3.5 在线生成一个工具调用
    → 检索 / 读表 / Decimal 计算 / 提交
    → 私有验证器给终局奖励
    → 同题 4 条轨迹的 GRPO 更新
    → LoRA checkpoint + episode/token 日志
```

默认机器为 **4090D × 1**，模型为 **Qwen/Qwen3.5-0.8B**。训练器使用 ms-swift，vLLM 与训练进程共用一张卡。当前实现了启动器和多轮插件，**尚未在 GPU 上执行训练，也没有训练效果或显存实测结果**。

## 1. GPU 环境安装

在 Linux NVIDIA 训练机的独立 Python 3.12 环境中，从仓库根目录执行：

```sh
python -m pip install -e .
python -m pip install -r agent/trainers/rl/requirements.txt
```

ms-swift 固定在 `2efb43ffe61294735be6816abca991fb2ca16011`，插件按该版本的多轮 hook、精确 token 与环境奖励接口编写。vLLM 的 CUDA/PyTorch 依赖由其安装包解析；需要匹配的 NVIDIA 驱动。普通数据构建和参考轨迹导出不导入 torch/vLLM。

接口来源：[官方 Gym 环境说明](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/docs/source/Instruction/GRPO/DeveloperGuide/gym_env.md)、[多轮执行器](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/swift/rollout/agent_loop.py)、[loss mask 语义](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/swift/loss_scale/base.py)。

## 2. 构建首轮任务并启动 RL

```sh
python -m agent.cli rl-build \
  --output data/agent/datasets/research-v1 \
  --train-cases 128 --validation-cases 32 --seed 42

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-01 \
  --max-steps 20
```

`rl-build` 不调用模型。它生成三个任务族：营收增长率、经营现金流占营收比例、比例变化的百分点数。公司、期间、数值、合并/母公司目标口径与表格顺序变化；另一口径的表格作为干扰项。默认 128 个训练任务、32 个验证任务，来源/快照/变体分组互不重叠。这是用于训练接线的合成数据，不能当作真实财报能力 Benchmark。

旧 `agent/examples/*/trajectory.json` 保持未执行的设计样例身份，不进入训练。新任务包有独立版本、文件 hash 与 split；启动时冻结本次使用的文件。后续真实财报任务构建承接已有治理结果，当前生成器只生成合成表格。

`rl --prepare-only` 只写快照与实际命令，不下载模型、不启动训练；请使用独立 output 目录。`--model` 可指定本地模型目录，`--config` 可指定修改后的 JSON 配方。

## 3. 可选：先学会工具协议，再做 RL

0.8B 模型直接做二值奖励 RL，可能出现同题四条轨迹全失败、优势为零。可先导出**实际执行并通过验证器**的参考工具示范，用现有 SFT 入口训练：

```sh
python -m agent.cli rl-sft \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/datasets/research-agent-sft-v1

python -m agent.cli sft \
  --dataset data/agent/datasets/research-agent-sft-v1 \
  --output data/agent/runs/research-agent-sft-01 \
  --config agent/trainers/rl/agent-sft-qwen3.5-0.8b.json \
  --modality text

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-from-sft-01 \
  --sft-adapter data/agent/runs/research-agent-sft-01/checkpoints/checkpoint-50
```

参考策略使用私有程序选择行动，但每个检索、读取、计算、提交都经过真实环境执行，并保存 episode；成功后才导出 SFT。教师轨迹明确标记 `on_policy=false`，不作为 GRPO 的在线采样。SFT 使用 `loss_scale=default` 学习所有 assistant 工具调用；工具观察为 user 消息，不进入损失。Agent SFT 专用配方长度 8192，避免使用普通 SFT 的 2048 默认长度截断工具轨迹。从 SFT 初始化 RL 时，同时把该 adapter 作为冻结的 KL reference；恢复训练时沿用并核对其文件 hash。

## 4. 奖励与边界

| 情况 | 奖励 | 处理 |
| --- | --- | --- |
| 数值、小数位、公司、口径、期间、支持单元格全部正确 | 1 | 有效成功 |
| 错答案、证据不符、未提交、动作/生成预算耗尽 | 0 | 保留有效失败 |
| 调用未授权工具 | -1 | 立即终止 |
| 宿主/推理/捕获失败 | null | 中止生成组，不将其计为策略失败 |

每轮一个 JSON 调用；参数错误消耗动作但可继续纠正。只能读取检索命中的表格，只能用已观察到的 cell/calculation ID 计算。计算记录保存 operation、operands 与展开后的单元格血缘；中间值使用 50 位 Decimal 精度，仅最终答案 ROUND_HALF_UP。提交只返回协议确认，评分和错误诊断不进入模型观察。

默认每题最多 8 个动作，单轮输出 512 tokens，总输出 4096；训练序列最多 8192，vLLM 容量 9216。超过训练序列长度时框架拒绝并重采样，不采用静默截断。训练器负责输入/输出的原生 token 编码和 assistant mask。当前只实现文本/结构化表格研究任务；图片观察、治理排障环境、真实财报 task adapter 与冻结 Benchmark 继续开发。

## 5. 日志、报告与恢复

```sh
python -m agent.cli rl-report --run data/agent/runs/research-rl-01

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-01 \
  --resume-from-checkpoint data/agent/runs/research-rl-01/checkpoints/checkpoint-10 \
  --max-steps 40
```

输出包含：

- `finflow-inputs/`：冻结的 task/fixture/oracle/工具表和训练/验证行。
- `finflow-run.json`：当前运行状态、解析后的配方、命令、依赖版本及源代码 hash。
- `attempts/*.json`：每次启动的不可变配置记录，恢复运行不会覆盖旧 episode 的绑定。
- `episodes/<id>/episode.json`：真实动作、观察、计算、终止原因、私有评分、策略 optimizer step、模型 revision 与数据绑定。
- `episodes/<id>/tokens.jsonl`：引擎原生采样 token IDs/logprobs，以及响应 token IDs/assistant mask；episode 绑定该文件 hash。模型原生捕获缺失时停止该生成组。
- `checkpoints/`：ms-swift 写出的 LoRA/训练状态；默认每 10 个 optimizer step 保存。

`rl-report` 按 split/训练阶段分别统计成功率、失败原因、未完成与基础设施失败。它是运行诊断，不能代替冻结评测。初版保留全零奖励组，不人为拼接成功/失败比例；如果持续全零，先使用上面的 Agent SFT 初始化并查看实际错误。
