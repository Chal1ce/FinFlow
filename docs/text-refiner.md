---
title: 可选小模型精炼插件
nav_order: 5.8
---

# 可选小模型文本精炼插件

精炼插件默认关闭，适用于希望尝试小参数模型清理训练文本的开发者。通过同一个 `.env` 选择模型、运行模式和重写策略，不自动安装推理服务、下载权重或训练模型。

## 1. 插件的位置与边界

```text
OCR → 图表归档 → 文本治理 → 原始候选 / 现有生成方法 → 审核
                         ↘ 可选精炼插件 → refined 候选 → 审核
审核通过 → CPT 增量 → 去重 / 配比 / 版本发布
原始证据 → 现有 SFT 流程
```

这是治理后的附加 CPT 分支，不替换现有清洗器，不覆盖原文或原图。插件从已有治理文本补跑，也处理每日新增的治理文本；关闭或切换插件模式不使 OCR/治理缓存失效。

- `audit`：记录操作建议、输出对照和血缘，不生成训练候选。
- `apply`：符合规则的结果生成 `method=refined` 候选，仍需独立审核、来源准入和既有训练发布规则。
- `delete`：只排除这条插件候选，不删除原文件、原始候选或历史数据。
- `rewrite`：需要额外开关，产生派生候选；不会变成 SFT 的事实证据。

原始方法继续运行，因此这不是整个训练池的过滤器。原始文本与精炼文本可能同时存在；相同内容由既有精确去重合并来源，近似重复由可选近似去重处理，也可按 `method` 配比。停用插件不撤销已经审核、发布的数据。

## 2. 配置

在现有 `.env` 中追加，保持其他 OCR、审核模型、tokenizer 和来源配置。不要覆盖整个文件。

```dotenv
FIN_DOC_REFINER_ENABLED=true
FIN_DOC_REFINER_MODE=audit
FIN_DOC_REFINER_ALLOW_REWRITE=false
FIN_DOC_REFINER_API_URL=http://127.0.0.1:8000/v1
FIN_DOC_REFINER_API_KEY=local-placeholder
FIN_DOC_REFINER_MODEL=your-served-refiner
FIN_DOC_REFINER_MODEL_VERSION=your-checkpoint-revision
FIN_DOC_REFINER_TIMEOUT=120
FIN_DOC_REFINER_MAX_TOKENS=3072
FIN_DOC_REFINER_MAX_INPUT_CHARS=12000
FIN_DOC_REFINER_MIN_RETAIN_RATIO=0.5
FIN_DOC_REFINER_PROTECT_FINANCIAL=true
FIN_DOC_REFINER_SYSTEM_PROMPT_FILE=
```

模型服务须兼容 OpenAI Chat Completions，且能输出下述操作协议。无鉴权的本地服务仍须提供非空占位 key，以满足 SDK 和预检；有鉴权的服务使用实际密钥。模型版本建议填写固定 checkpoint/revision。

默认使用 FinFlow 自带协议提示词；可通过 `SYSTEM_PROMPT_FILE` 指定 UTF-8 系统提示词，路径相对项目根目录或为绝对路径。文件内容纳入插件身份，不只记录文件名。正式模型需要其匹配的提示词、聊天模板和服务配置；仅修改模型名称不等于复现论文。

字符上限不等于模型 token 上限。输入先使用现有训练 tokenizer 分段；超过插件字符上限的片段记录为 skipped，不静默截断。仍需让服务的上下文窗口容纳系统提示词、行号输入及输出预算。插件请求与其他飞轮角色共用每日请求上限。

```bash
python -m workflow.flywheel_cli preflight
python -m workflow.flywheel_cli run --no-discover
python -m workflow.flywheel_cli refiner-status --limit 20
python -m workflow.flywheel_cli audit --method refined
python -m workflow.flywheel_cli report
```

`--no-discover` 不采集新来源，但仍会执行已有队列及本地 OCR 治理补登记。需要完整每日采集时去掉该选项。

确认记录符合需要后，将 `FIN_DOC_REFINER_MODE` 改为 `apply`；允许重写时再设置 `FIN_DOC_REFINER_ALLOW_REWRITE=true`。修改配置后重启常驻渠道进程。

关闭设置 `FIN_DOC_REFINER_ENABLED=false`：不读取插件模型/提示词配置，不创建或领取精炼任务，不发起精炼请求，不需要额外推理依赖。旧插件任务保留，恢复相同配置可续作；其他配方的插件任务暂停领取。已生成候选的审核仍由原有审核队列处理。

## 3. 操作协议与保护

输入逐行带 `<lid:1>` 等稳定标记，输出示例：

```text
<extract>
rm 1-2
<edit>
sub 4: "点击查看广告"
```

支持 `<extract>` 后接 `<keep>`、`<delete>`、`<edit>` 或 `<rewrite>`。也接受 ReScraper 的 decision-first 形式：先输出操作标签，再输出 extract 和同一个操作标签。`rm N` / `rm N-M` 删除行；`sub N: "…"` 删除原行内唯一匹配的 JSON 字符串，不是替换指令。所有行号均指向原始编号输入，不能按删除后的行号操作。

解析器拒绝越界、重叠、歧义子串、标签冲突、未知指令及截断响应；不会把解析失败的操作列表作为正文，也不会执行模型返回的代码。删除式操作保留剩余字符顺序。

- 含表格、公式或图片结构的片段跳过，继续使用原有图表流程。结构识别结合块类型和文本标记；仍依赖 OCR 标注质量。
- 默认检查数值、货币、日期中的数字、常见单位和否定词等信号。相关内容删除或信号改变会进入 `needs_review`。可关闭此启发式保护以适配其他领域，但独立审核仍执行。
- 非 delete 结果保留字符比例默认至少 0.5；可调整。该比例衡量长度，不证明语义保真。
- 无授权训练来源、未通过要求的 OCR 质量检查不会调用精炼模型。

这些规则不能证明事实无误。正文切段可能失去上下文，尤其要关注财务口径、表格注释和指代。当前不对图像像素调用小模型，也不自动进行中文领域微调。

## 4. 血缘、恢复与日报

本地 `processed/refiner/<task_uid>/` 保存 `response.json` 和 `result.json`（前置跳过时没有模型响应）。结果包含原文行号/字符范围、源 chunk 版本、解析操作、处理后文本、保护信号和模型配置身份。字符范围始终指向治理后的源证据，不冒充原始 PDF 或重写结果中的字符范围；页码/区域通过源 chunk 和 OCR 血缘追溯。

`refiner-response → refiner-result → training-candidate → training-decision` 注册 SQLite 工件及多父级血缘。apply 候选发布时其祖先随证据包导出；未产生候选的 audit/delete/skipped 记录只保留在本地，可用 `refiner-status` 获取工件 ID，再用 `trace --artifact-id` 查看。

任务身份绑定输入工件、片段、主流程配方、模型版本、提示词内容、模式和保护策略。同一配置复用已完成任务和响应；网络失败走已有重试，达到每日调用上限延后恢复。audit 切 apply 会产生新任务并重新推理，以免将审核前的结果静默发布。

无效输出保留为 `needs_review`，不产生候选。手动 retry 会复用已有响应；若需要重新生成，修正服务/提示词并更新模型版本或提示词内容。暂停任务保留在总队列中，但不计入当前可执行队列的状态。

日报记录本轮操作数、状态、候选数、缓存复用和输入/输出字符数；skip、invalid 与 delete 分开统计，避免把失败当成清洗收益。模型服务 usage 继续按角色记录，不估算供应商费用。

## 5. 方法来源与复现范围

参考 [ReScraper 论文](https://arxiv.org/abs/2609.34287)、[官方实现](https://github.com/cxcscmu/ReScraper)和[官方模型](https://huggingface.co/cx-cmu/ReScraper)。官方方法使用专门微调的 Qwen3-0.6B，在英文网页上联合完成正文提取和精炼。

FinFlow 当前独立实现严格操作协议，并接入治理后的金融 OCR 文本旁路；不包含原论文 HTML 渲染、教师数据构建、两阶段 SFT 训练或实验规模复现。默认提示词为 FinFlow 自有适配版，不保证官方 checkpoint 在中文金融文本上有效。替换模型时需要保持操作协议，并选择适配模型的系统提示词。上游代码和模型的许可请以各自仓库为准。

本插件目前完成静态检查，尚未进行功能测试或真实模型验收。
