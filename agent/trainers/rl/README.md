# 单卡金融 Agentic RL

当前代码提供这条训练链路：

```text
版本化任务/表格/私有 oracle
    → Qwen3.5 在线生成一个工具调用
    → 检索 / 读表 / Decimal 计算 / 提交
    → 私有验证器给终局奖励
    → 同题 4 条轨迹、4 道题/优化步的 GRPO 更新
    → LoRA checkpoint + episode/token/group 统计
```

默认配方使用 **Qwen/Qwen3.5-0.8B** 和单张 **4090D**。训练器基于 ms-swift，vLLM 与训练进程共用一张卡。

## 0. 准备实验

```sh
python -m agent.cli rl-start --output data/agent/experiments/research-01
```

默认生成 128 个训练任务、32 个验证任务和 0 个测试任务，实际执行确定性参考策略并导出 2 个变体的 Agent SFT 示范，冻结任务、示范和配方到同一个实验目录。该命令准备本地数据与训练输入，使用 `--train` 启动训练。`pipeline.json` 保存输入 hash、阶段状态和后续启动命令。

准备好的实验在训练机上执行：

```sh
python -m agent.cli rl-start --output data/agent/experiments/research-01 --train
```

该命令从基座模型开始 SFT，校验最终 LoRA checkpoint 的步数、`trainer_state.json`、配置和权重，再启动 GRPO。再次执行时跳过完成阶段；中断阶段有 checkpoint 时恢复，否则创建新 attempt。RL 阶段以匹配请求步数的最终 checkpoint 作为完成条件。

新实验可用 `--sft-steps 50 --rl-steps 200`、`--train-cases`、`--validation-cases`、`--seed` 调整规模；`--direct-rl` 跳过 SFT，直接从基座模型启动 RL。已有实验的输入和配方固定，修改参数时使用新的 output。自有研究任务使用 `--dataset`；已有治理表格使用下面的 `--sft-dataset`。

## 1. GPU 环境安装

在 Linux NVIDIA 训练机的独立 Python 3.12 环境中，从仓库根目录执行：

```sh
python -m pip install -e .
python -m pip install -r agent/trainers/rl/requirements.txt
```

ms-swift 固定在 `2efb43ffe61294735be6816abca991fb2ca16011`，插件按该版本的多轮 hook、精确 token 与环境奖励接口编写。启动 RL 时会核对安装的 ms-swift git commit 是否等于配方里的 `swift_revision`，不一致直接拒绝；非 git 安装会记录为未验证。vLLM 的 CUDA/PyTorch 依赖由其安装包解析；需要匹配的 NVIDIA 驱动。普通数据构建和参考轨迹导出不导入 torch/vLLM。

接口来源：[官方 Gym 环境说明](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/docs/source/Instruction/GRPO/DeveloperGuide/gym_env.md)、[多轮执行器](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/swift/rollout/agent_loop.py)、[loss mask 语义](https://github.com/modelscope/ms-swift/blob/2efb43ffe61294735be6816abca991fb2ca16011/swift/loss_scale/base.py)。

## 2. 构建任务、冻结测试集并启动 RL

```sh
python -m agent.cli rl-build \
  --output data/agent/datasets/research-v1 \
  --train-cases 128 --validation-cases 32 --test-cases 64 --seed 42

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-01 \
  --max-steps 200
```

`rl-build` 不调用模型。它生成四个任务族：营收增长率、经营现金流占营收比例、比例变化的百分点数，以及同时要求两个指标的双指标题。公司、期间、数值、合并/母公司目标口径与表格顺序变化；另一口径的表格作为干扰项。默认 128 个训练任务、32 个验证任务；`--test-cases 64` 生成不参与 SFT/RL 的冻结测试集。来源/快照/变体分组互不重叠。任务包标记为合成数据。

训练集按 `easy → medium → hard` 写入，配方使用 `dataset_shuffle=false`，因此前若干个 optimizer step 先看到简单题。验证/测试集不排序，也不参与梯度。

`agent/examples/` 保存接口设计样例。可执行任务包由 `rl-build` 或 `rl-import` 生成，包含版本、文件 hash 和 split；启动训练时将输入冻结到运行目录。

`rl --prepare-only` 只写快照与实际命令，不下载模型、不启动训练；再次对同一个 prepared output 执行 `rl` 会复用其输入和 adapter 绑定，不再报“目录非空”。`--model` 可指定本地模型目录，`--config` 可指定修改后的 JSON 配方。

## 3. 可选：先学会工具协议，再做 RL

0.8B 模型直接做二值奖励 RL，可能出现同题四条轨迹全失败、优势为零。可先导出**实际执行并通过验证器**的参考工具示范，用现有 SFT 入口训练：

```sh
python -m agent.cli rl-sft \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/datasets/research-agent-sft-v1 \
  --variants 2

python -m agent.cli sft \
  --dataset data/agent/datasets/research-agent-sft-v1 \
  --output data/agent/runs/research-agent-sft-01 \
  --config agent/trainers/rl/agent-sft-qwen3.5-0.8b.json \
  --modality text

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-from-sft-01 \
  --max-steps 200 \
  --sft-adapter data/agent/runs/research-agent-sft-01/checkpoints/checkpoint-50
```

`--variants 2` 尝试生成最小路径，以及先读干扰表再计算、提交精确证据的路径；第 3/4 个变体会换检索 query。基础路径必须通过验证；可选变体超出动作预算或无法检索所需表格时，跳过并在 manifest 的 `skipped_variants` 中记录原因，已通过的基础示范仍保留。参考策略使用私有程序选择行动，但每个检索、读取、计算、提交都经过真实环境执行；教师轨迹明确标记 `on_policy=false`，不作为 GRPO 的在线采样。

SFT 使用 `loss_scale=default` 学习所有 assistant 工具调用；工具观察为 user 消息，不进入损失。Agent SFT 专用配方长度 8192。从 SFT 初始化 RL 时，同时把该 adapter 作为冻结的 KL reference；启动时核对 adapter 的 `base_model_name_or_path` 与 `--model` 一致，恢复训练时沿用并核对其文件 hash。

### 3.1 用成功 rollout 做拒绝采样（可选）

如果 RL 中出现少量成功轨迹，可以把它们回收成 SFT：

```sh
python -m agent.cli rl-sft \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/datasets/research-agent-sft-v2 \
  --from-episodes data/agent/runs/research-rl-01/episodes \
  --per-case 1
```

只接受 `split=train`、`phase=train`、`reward=1`、最后一个动作是 `submit_answer`、且非终局动作没有工具错误的 episode；导出前会在冻结 case 上重放并再次验证。参考示范（`on_policy=false`）不会进入这个入口。

## 4. 从已有治理结果接入真实财报表格

```sh
python -m agent.cli rl-import \
  --dataset data/training/sft/financial-sft-v1 \
  --output data/agent/datasets/financial-research-v1

python -m agent.cli rl-start \
  --dataset data/agent/datasets/financial-research-v1 \
  --output data/agent/experiments/financial-01
```

也可一步准备：`rl-start --sft-dataset data/training/sft/financial-sft-v1 --output data/agent/experiments/financial-01`。输入必须是已有 `financial-sft-dataset-v2` 快照；示例路径换成实际目录。仅消费 `decision.status=accepted` 的 `table_structure` 样本，复用原 work/content/image 划分、审核工件和来源关联，不重新生成或审核表格。

首版自动支持：

- 年报元数据中的股票代码和年度；表头明确写出的报告年/上一年。
- 表格标题明确标出的合并或母公司口径；单位明确标出的人民币及元/万元/百万元/亿元。
- 营业收入、经营活动现金流量净额；同口径、同单位的利润表与现金流量表可以联合计算。
- 营收增长率、现金流占营收比例、跨期比例变化。空白/横线不当作零，不猜单位和期间；冲突值、缺字段和不支持的格式记录到 `excluded.jsonl`，没有可用训练题时返回原因计数。

任务中的数值不是重新编造的；fixture 标记 `synthetic=false`。题目和参考答案由模板/Decimal 程序构建，观察内容是**已审核表格转录的数值行投影**，继承其现有质量。每个 case 绑定 `provenance.json`，保存原 sample/evidence、图像 hash/工件、审核身份、原始字符串和从零开始的行列坐标；训练快照同时复制这些绑定文件。RL 观察使用结构化表格数值行，图片通过 provenance 关联。

治理表格入口支持 3 个单指标族：营收增长率、现金流占营收比例和跨期百分点变化；合成任务另外提供双指标族。

## 5. 奖励规则

| 情况 | 奖励 | 处理 |
| --- | --- | --- |
| 数值、小数位、公司、口径、期间、支持单元格全部正确 | 1 | 有效成功 |
| 数值正确，但多提交了已观察的证据 cell | 训练 1；严格评测 0 | `all_checks_passed_extra_evidence` / `evidence_extra` |
| 数值正确，但小数位不是要求的位数 | 0 | `numeric_format`，与 `numeric_value` 分开 |
| 错答案、证据不符、未提交、动作/生成预算耗尽 | 0 | 保留有效失败 |
| 未知工具名（拼写/幻觉） | 0，可继续 | `unknown_tool`，消耗一个动作，不立即终止 |
| 已知工具但本题未授权 | -1 | `forbidden_action`，立即终止 |
| 宿主/推理/捕获失败 | null | 中止生成组，不将其计为策略失败 |

训练时 `strict_evidence=false`：只要提交的证据展开集合包含某个 `evidence_alternative` 就通过，避免“多读了一张干扰表”被误判。冻结评测默认 `strict_evidence=true`，同时记录 `strict_support`。

每轮一个 JSON 调用；参数错误消耗动作但可继续纠正。只能读取检索命中的表格，只能用已观察到的 cell/calculation ID 计算。`read_table` 可选 `period` 过滤单年。计算记录保存 operation、operands 与展开后的单元格血缘；中间值使用 50 位 Decimal 精度，仅最终答案 ROUND_HALF_UP。提交只返回协议确认，评分和错误诊断不进入模型观察。

默认每题最多 8 个动作，单轮输出 512 tokens，总输出 4096；训练序列最多 8192，vLLM 容量 9216。超过训练序列长度时框架拒绝并重采样，不采用静默截断。训练器负责输入/输出的原生 token 编码和 assistant mask。

## 6. 配方与训练规模

当前 `qwen3.5-0.8b.json`：

- `num_generations=4`、`generation_batch_size=16`、`gradient_accumulation_steps=16`：一个 optimizer step 覆盖 **4 道题 × 4 条轨迹**。
- `vllm_max_num_seqs=16`：16 条轨迹并发生成；24GB 显存不足时先降回 8/8。
- `dynamic_sample=true`、`max_resample_times=3`：组内奖励方差为 0 的组会被重采样。
- `max_steps=200`、`learning_rate=1e-5`、`warmup_ratio=0.03`、`save_steps=50`、`eval_steps=50`。
- `per_device_eval_batch_size=4`、`num_generations_eval=4`：验证集每题采样 4 条轨迹并统计 pass@4。
- `beta=0.02`、`scale_rewards=group`、`loss_type=grpo`、`log_entropy=true`。

## 7. 冻结评测

```sh
python -m agent.cli rl-eval \
  --dataset data/agent/runs/research-rl-01/finflow-inputs \
  --output data/agent/evaluations/research-01 \
  --split test --policy reference --samples 2
```

用 run 目录里的 `finflow-inputs/` 而不是原始数据集，可以确保评测用的是这次 run 冻结的 case 文件。

`--policy reference` 用确定性参考策略验证评测链路，不需要 GPU；`--policy replay --responses actions.jsonl` 重放日志里的动作（每行 `{"case_id":"...","actions":["{...}", ...]}`）；`--policy model --model ... --adapter ...` 用本地 transformers/peft 模型逐回合生成。评测默认严格证据，输出 `eval.json`，包含 pass@1、pass@k、bootstrap 95% CI、每族/每难度结果、失败原因和动作数。测试集不参与 SFT/RL。

## 8. 日志、报告与恢复

```sh
python -m agent.cli rl-report --run data/agent/runs/research-rl-01

python -m agent.cli rl \
  --dataset data/agent/datasets/research-v1 \
  --output data/agent/runs/research-rl-01 \
  --resume-from-checkpoint data/agent/runs/research-rl-01/checkpoints/checkpoint-100 \
  --max-steps 300
```

输出包含：

- `finflow-inputs/`：冻结的 train/validation/test 任务、fixture、oracle、工具表；先写临时目录再原子 rename，准备失败不会留下半成品。
- `finflow-run.json`：当前运行状态、解析后的配方、命令、依赖版本/commit、源代码 hash、开始/结束时间。
- `trainer.log`：ms-swift 的完整 stdout/stderr，同时流式打印到终端。
- `attempts/*.json`：每次启动的不可变配置记录，恢复运行不会覆盖旧 episode 的绑定。
- `episodes/<id>/episode.json`：真实动作、观察、计算、终止原因、私有评分、optimizer step、request id、attempt、模型 revision 与数据绑定。
- `episodes/<id>/tokens.jsonl`：引擎原生采样 token IDs/logprobs、响应 token IDs/assistant mask、mask token 数、上下文长度；episode 绑定该文件 hash。模型原生捕获缺失时停止该生成组。
- `checkpoints/`：ms-swift 写出的 LoRA/训练状态；默认每 50 个 optimizer step 保存。

`rl-report` 按 attempt、split、phase、难度和任务族统计结果、工具错误码与格式修复次数。组级指标按 `(attempt, split, phase, optimizer_step, case)` 聚合，包含全零组、全一组、零方差组、`pass@k` 和平均奖励；同一步内重复采样的同题归为一组。环境或奖励版本不匹配的 episode 单独计数。冻结测试集评测使用 `rl-eval`。
