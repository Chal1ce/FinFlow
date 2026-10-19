# 合成案例与契约说明

这四个案例用于审阅接口和实现边界。全部数据为人工构造，轨迹中的调用和返回是预期交互示例，没有运行环境或模型。`design_example` 必须保持 `verifier_status=not_executed`、`reward=null`、`task_success=null`、`train_eligible=false`。

这四个案例仍采用 V0.1 文本/结构化协议；已确认的页面与 OCR 区域图像扩展见[多模态设计](../multimodal.md)，相应媒体 schema 和视觉 fixture 尚待 V0.2 实施。

| 目录 | 覆盖范围 | 预期业务结果 | 示例调用数 |
| --- | --- | --- | --- |
| [research-001](research-001/task.json) | 同报告内合并/母公司口径干扰、两期占比计算 | 合并口径增加 2.00 个百分点 | 6 |
| [ops-001](ops-001/task.json) | 派生文件缺失、可信副本恢复、恢复后复验 | repaired | 6 |
| [ops-002](ops-002/task.json) | 正常批次对照 | no_action | 3 |
| [ops-003](ops-003/task.json) | 来源训练授权待人工决定 | escalated，授权状态不变 | 4 |

每个目录包含 `task.json`、`fixture.json`、`oracle.json`、`case.json` 和 `trajectory.json`。Agent 只接收 task 与允许工具的返回。宿主读取其他文件；真实任务不得把案例目录、oracle、分组标签或恢复库直接挂入模型可访问的文件系统。

## Schema 与宿主语义

五个 JSON Schema 使用 Draft 2020-12。`tools.json` 中每个 `input_schema` 约束工具参数，全局 `observation_schema` 约束返回信封。每个工具的 `response_data_contract` 说明业务字段；这些字段的完整逐工具输出 schema 在 P1 实现时补齐并版本化。`observation.data` 当前只要求 object，结构校验不能替代业务验收。

`fixture.schema.json` 表示 V0.1 的规范化合成初态，后续 builder 据此创建 SQLite、工件清单和文件；它不是现有 `pipeline.db` 的直接导出。`initial_content_utf8=null` 表示初始文件缺失；非空文本按 UTF-8 原样写入，包含末尾换行。`expected_sha256` 来自可信登记内容，恢复不能修改它。`published`、`restore_allowed`、`protected` 共同决定恢复是否可执行。恢复源内容只留在宿主恢复库。

`lineage_edges` 定义工件关系；源策略、候选和任务是语义种子，由 builder 映射到受控数据库。所有 ID 只在该 episode 范围内解析，路径相对于独立工作目录；还必须执行真实路径包含性和符号链接检查。

`case.files.*.path` 相对于 `agent/`，文件 sha256 对原始字节计算。修改 task、fixture、oracle 或 tools 后须重新绑定 case；修改 case 后须重新绑定 trajectory。线上工件采用各自发布包根目录，保持相同的相对引用规则。轨迹的 `state_fingerprint=null` 表示设计样例尚无执行状态，不代表一个真实状态 hash。

research 样例的 cell ID 由规范来源内容生成：对 `{document: 文档元数据（不含 tables）, table_id, scope, rows: 不含 cell_id 的行数组}` 使用 `json.dumps(ensure_ascii=False, sort_keys=True)` 的默认分隔符编码为 UTF-8 后做 SHA-256，得到 `cell:<hash>:<零起始行序号>`。这是样例规则；生产 adapter 的内容规范化、重复表格身份和 cell UID 算法需独立版本化，不能依赖易变页码作为唯一身份。

JSON Schema 不负责以下跨文件断言：task/track 身份一致、工具属于当前任务、文件 hash 正确、证据曾被观察、状态版本递增、预算不超限、分组不跨 split、计算语义和保护集合完整。实现时由 contracts、runner 与 verifier 分别负责，任何一项失败都不能仅凭 JSON 合法而放行。

## Oracle 谓词

所有 case 还需通过通用检查：提交处置属于 `accepted_outcomes`、没有禁止动作或保护集合破坏、引用确实在本 episode 中被观察，且未发生基础设施失效。谓词是对最终状态和动作审计的断言，不指定唯一调用顺序。

| kind | 验收语义 |
| --- | --- |
| research_result | 参考程序用高精度 Decimal 执行，最终 ROUND_HALF_UP 到指定小数位；同时检查单位、实体、期间、口径及至少一组完整等价证据 |
| evidence_observed | 所有提交引用均来自该 episode 工具观察；不代表引用必然支持答案 |
| insufficient_evidence | 宿主确认缺少指定必要材料，且提交为材料不足；不能凭模型自述通过 |
| artifact_integrity | 直接读取最终文件字节，与 oracle 中可信 hash 比较；不接受 Agent 提供的新 hash |
| post_write_verification | 最后一次影响目标的写操作之后，Agent 调用了覆盖目标的完整性校验且观察为匹配；最终 verifier 仍独立读文件 |
| task_successor_completed | 原任务由合法替代任务完成，依赖重接正确，成功工件唯一；仅返回 job_id 不满足 |
| protected_unchanged | 与初态比较保护对象的语义状态/文件字节；排除的审计字段由 fingerprint 版本明确列出 |
| policy_unchanged | 来源准入及相关人工决定保持原值 |
| no_pipeline_mutations | 没有修改任务、候选、工件、来源策略、发布数据；允许隔离的审计、幂等 journal 与本地 escalation 记录 |
| escalation_exists | 存在匹配对象和 reason_code 的本地转交记录，证据可解析、支持该理由；创建真实外部工单不在本版范围 |
| no_running_tasks | 终止时当前 scope 无仍在执行的任务，不能遗留后台处理冒充完成 |

`ops-001` 与 `ops-002` 的基础快照组相同，未来作为一个分组分配数据划分；不能把缺失/健康变体分别放进训练集和测试集来宣称独立泛化。

## 后续必须覆盖的拒绝案例

P1/P2 增加：错误期间或母公司数值碰巧正确、未读证据直接引用、除零、材料不足误弃答；任意路径、修改期望 hash、覆盖已发布/健康对象、重复 request_key 更换参数、过期 state_version、排队后直接提交 repaired、修复后未复验、可修复案例直接转人工、正常案例无效重试、私自批准来源、越界访问，以及真实基础设施失效。

另外增加暂时性处理失败的 retry→限定执行→独立复验案例，覆盖现有队列替代和依赖重接语义。它需要有界 stage adapter，当前四个设计案例未演示其实际执行。
