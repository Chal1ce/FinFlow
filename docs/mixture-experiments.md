---
title: DoReMi 与 RegMix
nav_order: 5.6
---

# DoReMi 与 RegMix 配比实验

这里会执行真实的小型因果语言模型训练，以验证损失学习数据配比。
代码是单设备、从零初始化的小型 Transformer 适配版，未复现原论文的模型规模或报告成绩。
日常飞轮不会自动启动这些实验；大型目标模型的训练仍交给下游训练程序。

## 1. 依赖和数据准备

```bash
python -m pip install -e '.[mixture-training]'
cp config/mixture-experiment.json config/mixture-experiment.local.json
```

填写已有 CPT v2 快照路径、分组字段、同一个 `tokenizer.json` 路径及真实 `eos_token_id`。
模板 EOS 为 null，需要按 tokenizer 配置填写，不能随意用 0。
每个域都必须已有训练和验证数据；不会临时把训练文档切入验证集。

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json preflight
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  prepare --output data/training/domains/finance-001
```

`preflight` 只检查路径和依赖是否存在；`prepare` 再校验输入包、分组、tokenizer、split 和 EOS。
域包包含 tokenized train/validation、来源样本和 token 偏移、tokenizer、manifest 和 checksums。
训练使用文档内固定长度块，**丢弃不足一个训练序列的尾部**以保证不同配比试验有相同 token 预算；
验证保留短尾，padding 标签不计损失。manifest 报告各域丢弃 token 数。
如果一个域没有完整训练块，减小 sequence_length 或扩大数据后创建新的域包。
默认最多准备一千万目标 token；当前实现将域包读入内存，适用于代理实验子集。

## 2. DoReMi

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  doremi --data data/training/domains/finance-001 --output data/experiments/doremi-001
```

实际步骤：

1. 按 `initial_weights` 训练参考模型，缺省为各域均匀权重。
2. 从相同初始化重新创建代理模型，冻结参考模型。
3. 每一步从每个域取等量完整训练块，计算逐 token 的 `max(proxy_loss - reference_loss, 0)`。
4. 对每个域求平均，执行指数权重更新及均匀平滑；按更新后的域权重训练代理模型。
5. 对整个代理阶段的权重取算术平均，导出 `weights.json`。

使用每域等量的分层 batch，避免非均匀抽样的校正问题。`batch_size` 在此表示**每个域**的 batch 大小，
每步处理量是域数 × batch_size × sequence_length；参考模型训练的 batch_size 为总 batch。
DoReMi 的两阶段 token 预算因此分别报告，不能只比较 steps。

可调 `proxy.doremi_eta`、`proxy.doremi_smoothing`、`reference_steps`、`steps`。
保存 `reference.pt/json`、`doremi.pt/json`，包含模型/优化器、权重累积、步数、损失记录和每域 token 暴露量。
实现依据：[官方代码](https://github.com/sangmichaelxie/doremi/blob/7cde52d1848737aa967ecbdb9e643cf334de160d/doremi/trainer.py)、
[论文](https://arxiv.org/abs/2305.10429)。

## 3. RegMix

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  regmix --data data/training/domains/finance-001 --output data/experiments/regmix-001 --max-trials 4
```

同一命令可以继续未完成实验。`--max-trials` 只限制本次新增完成的设计试验数；
全部设计试验完成后，命令还会拟合回归器并完成一次确认训练。去掉该参数可运行到结束。

实际步骤：

1. 固定 seed 生成均匀、自然规模、平方根配比、单域配方及 Dirichlet 随机配方。
2. 各配方使用相同模型初始化、训练步数、完整序列长度和冻结验证块训练代理模型。
3. 以固定验证目标的平均交叉熵为标签，拟合 LightGBM；按完整试验留出一部分数据，报告 RMSE、均值基线 RMSE 和 Pearson。
4. 在候选配比上搜索预测损失较低的方案。
5. 对建议方案执行真实确认训练；若没有胜过已测最佳配方，则导出已测最佳配方。

默认 32 个设计试验、4096 个随机搜索候选。试验数至少为 `max(12, 域数+3)`；
这些是入门配置，不代表原论文规模或足够的统计稳定性。
输出 `design.json`、`trial-*.pt/json`、`regressor.txt`、`confirmation.pt/json`、
`regression-report.json` 和 `weights.json`。回归报告包含留出误差、候选建议、确认损失和最终选择依据。
实现依据：[官方仓库](https://github.com/sail-sg/regmix/tree/dd9d1c3b2d7c1756b1a90f0ad7603068e9856cc6)、
[论文](https://arxiv.org/abs/2407.01492)。

## 4. 验证目标、设备与恢复

- `validation_weights` 可按域指定验证目标，必须列出所有域，允许某些域为 0；默认每域均匀。
- 每域验证块按稳定 ID 排序截取，最多 `eval_blocks_per_domain`，所有试验共享。
- `proxy.device` 支持 `cpu`、`cuda`、`mps`，默认 CPU。不会下载模型或 tokenizer；GPU/设备须已可用。
- 代理模型为小型 pre-norm 因果 Transformer，共享输入/输出词嵌入、学习位置嵌入、dropout=0、AdamW。
- `checkpoint_every` 控制保存周期，默认 50 步。中断后重跑完全相同的配置和 output；最近检查点之后的步骤会重做。
- 实验身份绑定数据 manifest、配置、实现版本和 PyTorch 版本；改变这些条件应使用新输出目录。
- 启用确定性算法，不支持的设备算子会报错；不同硬件/库版本不承诺逐位一致。
- 代理训练有放回抽样，结果报告实际 token 数和每域等效 epoch。静态训练包导出无放回，容量不足时会报告。

`run.json` 记录固定验证块、配方与方法卡。RegMix 的留出试验用于检查回归器预测能力；
所有验证损失仍用于配比调优，**不是独立最终测试成绩**。真实目标模型上是否提升，需要另做下游训练评估。

## 5. 导出实际训练包

```bash
cp config/mixture-learned.json config/mixture-learned.local.json
# 配置 method=doremi 或 regmix、对应 weights_file，以及同一输入池和分组。
python -m training.mixture_cli --config config/mixture-learned.local.json plan
python -m training.mixture_cli --config config/mixture-learned.local.json \
  build --output data/training/mixtures/learned-001
```

详细的容量不足处理、图片导出和数据契约见[数据配比与选择](data-mixtures.md)。
这套接口已实现算法流程；仓库尚未提供此项目真实语料的训练结果或原论文结果复现记录。
