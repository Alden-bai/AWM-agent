# AWM Harness 设计方案 v1：旧方案审查与迁移

日期：2026-10-23。状态：设计稿，未实现、未运行训练或新模型请求。

> 实现进度更新（2026-10-23）：P0–P3 已在 `harness_awm/` 实现并完成离线验收；原文保留为设计基线。使用方式见 [Harness 使用说明](harness_readme_zh.md)，实际验证及未验证边界见 [验收报告](harness_validation_zh.md)。P4 GRPO 与 P5 过程校准尚未实现，未调用付费模型。

用户已确认以 AWM 为主迁移。审查对象为附件 `daily_personal_agent.md`，重点为第七部分 Harness，同时检查与 rubric、Judge、reward 和数据划分的接口。本方案不改变现有两层清洗流程，Harness 消费其版本化产物。

## 1. 结论与直观例子

旧设计的方向可以保留：一个共享执行内核服务 Teacher 采集、模型评测、GRPO 在线采样；每条轨迹独立环境；执行、评测和诊断分层；失败留痕。它还不足以直接开训，主要缺少 AWM 的动作、终态、评分和训练数据合同。

用一个示意任务说明：用户要求将商品 A 的价格改为 80、库存改为 20。

1. 调度器领取冻结任务，为这条轨迹复制初始数据库并启动专属 MCP 服务。
2. 模型发现工具，通过合法查询取得商品 ID，再调用修改工具。
3. Harness 保存模型实际看到的输入、原始动作、工具返回；数据库仍由环境管理。
4. 如果价格已改成功，库存修改超时，不能把两个操作全重发。先确认旧请求已经结束或无法继续写入，再允许模型查询状态决定下一步。
5. 模型明确结束或达到预算后，Harness 禁止新动作、等待／终止在途执行并确认静止，保存不可变终态。
6. 官方 SQL+LLM Judge 检查真实轨迹、最终答复和初末数据库；RewardAdapter 将有效结果映射为 1 或 0，无效结算暂不给分。
7. 过程 Judge 另查“修改前是否有对象依据”等已冻结规则。结果正确与过程可靠分别报告。

这套系统证明的是某次执行在指定环境、协议和评测配置下的表现；日志齐全、两位 Judge 同意或任务通过有限测试，都不等于所有任务的语义正确性已获证明。

## 2. 旧方案哪些保留，哪些需要改

| 旧设计／表述 | 判断 | AWM 版处理 |
|---|---|---|
| 一个内核服务三个阶段 | 保留 | 共用 EpisodeRunner，阶段只改变调度、数据准入和导出 |
| 任务冻结、独立世界、失败留痕 | 保留并具体化 | 固定初态、时间、代码和工具契约；逐轨迹独立 DB、进程、会话和目录 |
| 每轮一个 Python 代码块、保留变量 | 替换 | v1 使用 MCP 结构化动作；不开放通用 Python 执行。确有计算需求再增加版本化受限工具 |
| 只需权限、上下文、并发等少数能力 | 不够 | 还需要任务准入、动作合同、可靠结算、证据、奖励和训练导出 |
| 超上下文就丢弃 | 必须改 | 停止生成，保留记录并尝试结算；不能从评测分母删除困难轨迹 |
| “输入加预留输出小于上限就丢弃” | 条件写反 | 超过上限才停止；等于上限是否允许由已冻结服务合同决定 |
| 模型标称上下文约 26 万 | 不能作为部署预算 | 采用实际服务窗口、训练序列能力及实测显存的共同约束；本次不确认该型号规格 |
| 历史不能压缩，否则不能 GRPO | 过强 | 可以在线改变后续上下文，但必须保存每轮真实输入；禁止事后改写采样条件 |
| 环境 seed 保证相同初态 | 不充分 | seed 是来源记录，冻结快照及状态校验才是初态一致证据；时间和外部依赖也要固定 |
| LLM 看完轨迹再分 required/quality/diagnostic | 与预先冻结矛盾 | 规则类别、适用条件、权重、检查器在看候选之前冻结；Judge 只判状态 |
| 原生结果是确定性 0/1 | 不适用于 AWM SQL 路径 | SQL 取证后还要调用结果 LLM Judge，需先校准指定配置 |
| 原生漏检以后再补 | 研究边界不足 | 可以报告官方通过率，但不能把已知漏检配置直接称为可靠训练奖励 |
| AppWorld 一律查不到重复收款 | 附件证据不足 | 必须逐任务检查测试；不能由单例推断整个环境。迁移后不沿用该断言 |
| 已经接入 GRPO | 附件中完成态与设计态混杂 | 本文仅提供方案；接口、token、概率、训练更新都需实际验收 |
| J1/J2 一致即可判可靠 | 不充分 | 独立参考标签、正反例和分层抽查校准；一致率单独报告 |

“调用过查询”也不等于“行动有依据”。例如只查到第一页不能证明某人未付款；Judge 必须检查查询范围、分页和行动之前的证据。

## 3. 当前本地源码依据

本次读取的官方源码 HEAD：`85e322f69279e3b3325b7377ec3bab788514e9cb`。这是代码版本，不能拿数据目录名代替它；正式运行另存工作区差异及依赖版本。

| 事实 | 当前来源 | 设计影响 |
|---|---|---|
| 官方示例使用 list_tools/call_tool，纯文本结束 | [agent.py](/home/bjx/awm-agent/agent-world-model/awm/core/agent.py:88) | 改为明确可验证的动作／结束协议，并标为自定义运行协议 |
| 解析器跳过部分非法调用；循环无调用则结束，多调用仅执行首个 | [agent.py](/home/bjx/awm-agent/agent-world-model/awm/core/agent.py:130)、[循环](/home/bjx/awm-agent/agent-world-model/awm/core/agent.py:505) | 不直接照搬示例运行器的控制逻辑 |
| 通用 JSON 解析函数能将字符串解释为文件路径 | [tools.py](/home/bjx/awm-agent/agent-world-model/awm/tools.py:296) | 模型动作只用严格内存 JSON 解码，不使用该文件读取能力 |
| 工具包装将返回转为文本；isError 转为文本前缀 | [agent.py](/home/bjx/awm-agent/agent-world-model/awm/core/agent.py:316) | 另存结构化原始 MCP 结果、传输状态、业务结果 |
| 工作 DB 可命名为 final.db，末尾复制到输出 final.db | [server.py](/home/bjx/awm-agent/agent-world-model/awm/core/server.py:54)、[agent.py](/home/bjx/awm-agent/agent-world-model/awm/core/agent.py:595) | 显式区分 initial/working/final；禁止源目标同路径；不依赖复制活跃 DB 得到一致快照 |
| 初始化有错误时可能警告后仍返回路径 | [reset.py](/home/bjx/awm-agent/agent-world-model/awm/core/reset.py:104) | “文件存在”不等于环境准备通过 |
| SQL 模式的中间 reward_type 可为 incomplete，随后才产生最终分类 | [verify.py](/home/bjx/awm-agent/agent-world-model/awm/core/verify.py:201)、[调用](/home/bjx/awm-agent/agent-world-model/awm/core/verify.py:417) | 读取 llm_judge.classification，不能读取中间字段当奖励 |
| SQL Judge 接收 trajectory；messages 中最终答复未被单独作为 SQL Judge 参数传入 | [verify.py](/home/bjx/awm-agent/agent-world-model/awm/core/verify.py:280)、[适配](/home/bjx/awm-agent/agent-world-model/awm/core/verify.py:392) | 最终答复必须进入真实 trajectory 的 is_final 记录，并验证实际请求 |

以上为静态源码检查，不等于这些分支本次已经动态复现。当前清洗边界见 [两层清洗方案](/home/bjx/awm-agent/docs/two_layer_cleaning_plan_zh.md:1)。

## 4. 总体架构与职责

```text
冻结任务库 + 运行配置 + 已验收的评测配置
                    ↓
阶段运行与批量调度：collect / evaluate / rollout
                    ↓
EpisodeRunner
  ├─ ContextManager → ModelBackend → ActionParser
  ├─ ToolGateway → AWMEnvAdapter → 专属 MCP 服务 + working.db
  └─ EventRecorder：逐轮输入、输出、调用、可见性与状态引用
                    ↓ 停止动作、确认静止、保存终态
OfficialOutcomeEvaluator（官方 SQL + LLM）
  ├─ RewardAdapter → GRPOGroupAssembler → 训练导出
  └─ ProcessEvaluator（离线 rubric/Judge）→ SFTSelector

独立 diagnose：读取证据，在新世界复现，不修改原轨迹
```

这些是逻辑边界，MVP 不必部署成十几个服务。可实现为一个 Python 包，环境执行与私有评测分别放在隔离进程中。

| 模块 | 必须承担的责任 | 关键输出 |
|---|---|---|
| TaskRegistry | 校验任务／环境／verifier 绑定、划分、来源及适用能力 | TaskSpec、准入结果 |
| StageScheduler | 预算、并发、候选组、背压、阶段统计 | run/group/candidate 标识 |
| EpisodeRunner | 唯一生命周期管理、顺序推进、停止与结算 | EpisodeResult |
| ModelBackend | Teacher API 或训练 Actor 接口；保存真实输入输出 | 文本／token、用量、模型版本 |
| ActionParser + ToolGateway | 严格解析、工具 schema 校验、授权和调用关联 | 合法动作或显式格式错误 |
| AWMEnvAdapter | 初始化、服务启动、工具发现、隔离、静止、快照、销毁 | EnvHandle、快照及状态 |
| ContextManager | 真实上下文、空间预算、公开观察外置与读取 | 每轮输入与可见性账本 |
| EventRecorder | 事件顺序、原始材料、引用、持久化和校验 | 追加式事件流 |
| Evaluation/RewardAdapter | 原版结果评测、有效性判断、版本化奖励映射 | 原始分类、有效标记、reward |
| ProcessEvaluator / Selector | 冻结过程规则、证据包、离线筛选 | 逐项状态、Q、accept/review 等 |
| Exporter / GroupAssembler | SFT mask、GRPO 真实采样条件、整组资格 | 可追溯训练样本 |

## 5. 输入与版本合同

TaskSpec 至少保存：

```text
protocol_id, dataset_version, split, lineage_group_id
scenario, native_task_id, task_instance_id, instruction
initial_state_ref/hash, environment_code_hash, tool_schema_hash
verifier_hash, judge_config_hash, readiness_profile
world_datetime, timezone, clock_policy
```

运行另存 `run_id / group_id / candidate_id / rollout_id / attempt_id`、模型权重版本、采样参数、seed、提示模板、tokenizer、动作协议、观察处理策略和全部预算。

- native_task_id 保持原始类型和含义；task_instance_id 用于本项目的全局唯一定位。
- 相同任务的模型自行改参数、纠错，仍是原 attempt；环境丢失从头重跑才增加 attempt，并保留替代关系。
- 环境可启动、题目保留、奖励配置通过检查，是三个不同状态。开发冒烟允许 pending 任务；正式训练只消费对应能力通过的任务版本。
- 一个环境下不同任务可能共享初态。先按来源族和初始世界划分，再采样轨迹；不能只随机切 task_id。
- 若研究目标是同类业务的新世界泛化，须先生产并验证独立世界及其 verifier 绑定；若当前资产做不到，应说明只能评测更窄的泛化目标。不能把轨迹多采几次说成新增任务实例。

## 6. 动作协议

v1 建议每轮只允许一个严格 JSON 对象，类型是以下之一：

```json
{"type":"list_tools"}
{"type":"call_tool","name":"<发现的工具名>","arguments":{"key":"value"}}
{"type":"read_observation","observation_id":"obs_17","offset":0,"limit":1000}
{"type":"finish","answer":"面向用户的最终答复"}
```

上面是四个独立示例，不是一次输出四个对象。内部动作协议与模型 API 的 native tool calling 可以通过适配器对应；统一冻结序列化方式。

- 首次通过 list_tools 获得真实工具目录；重复发现不会执行写入，记录成本，是否低效交给离线评价。
- 全量解析后再执行；多对象、混合 finish 和调用、缺字段、未知工具、非法参数返回 protocol_error，任何业务动作都不执行。错误占用轮次／token 预算。
- 不自动取第一个调用，不修复参数，不把没有成功解析的输出当 finish。
- 调用成功不等于任务成功；finish 只表示模型声明结束，answer 允许为空但要按题意评估。
- read_observation 是 Harness 的公开辅助工具，限本轨迹已获准的观察；独立记录，不能伪称来自 AWM。
- 官方评测适配按原生记录形状输出真实 tool_calls/tool_response 和 `is_final` 答复。保留模型原始 JSON 和解析结果，不增加答案或杜撰成功文本。
- 这是自定义 Actor 协议，不能宣称与官方默认 Agent 配置完全相同；结果 Judge 的原始 verifier、提示词和输入构造保持原版。

MVP 不加入规划 Agent、自动代码修复器或长期记忆。以后增加任何助手模块，都作为 Agent 系统能力计入实验配置和成本。

## 7. 生命周期、资源预算和故障

```text
CREATED → PREPARING → RUNNING → STOPPING → SNAPSHOTTING
        → EVALUATING → SETTLED
```

初始化失败可在无终态情况下结束；评测临时失败进入 EVAL_PENDING，对同一终态补评。状态记录与业务成败分开。

EpisodeResult 至少分开记录：

```text
termination_reason, termination_origin
execution_integrity, terminal_state_status
evaluation_status, outcome_classification
reward_valid, reward, process_status, q
```

停止原因包括模型 finish、最大轮次、token／墙钟预算、上下文空间不足、用户取消和不可恢复环境故障。工具参数错误通常仍在 RUNNING；不能遇到一次错误就判整题失败。

必须配置：每轮最大输出、最大模型轮次、最大工具调用、每次调用超时、episode 总时限、输入窗口、累计 token、环境并发、模型并发、Judge 并发及全局付费请求上限。官方工具本身的执行超时与客户端等候超时分别记录。

| 事件 | 处理 |
|---|---|
| 模型请求未获可用输出 | 对同一输入按冻结上限重试；保留每次请求，不挑最好输出 |
| 模型输出被截断 | 不执行不完整动作；作为模型可见错误或按预算停止，保留原输出 |
| 工具参数／业务错误 | 真实返回模型；其自行纠错属于同一轨迹 |
| 写入超时，是否提交未知 | 不自动重发；确认在途调用不会继续写，再允许合法查询。无法确认则停止、标记状态不可靠 |
| 明确的暂时读请求失败 | 仅对已注册为无副作用且允许重试的调用按固定策略重试；记录每次尝试 |
| 环境崩溃 | 若无法建立可信终态，reward 无效；保留旧尝试，新尝试从冻结初态启动 |
| Judge 服务异常 | 只补评原不可变输入；不重跑模型，不“重判到通过” |
| 进程在模型响应后、工具执行前崩溃 | 以持久化事件为准；不确定执行过的写动作不得盲重放 |

MVP 不承诺精确恢复任意执行现场。跨进程崩溃存在“已写 DB、完成事件未落盘”的窗口：记 outcome_unknown，依靠独立状态证据核查，不能伪造 exactly-once 保证。

## 8. 隔离与快照

每条候选分配专属 working.db、MCP 服务进程／端点、临时生成代码路径、客户端会话、输出目录和事件流。四条 GRPO 候选可以排队运行，不必同时占四个 worker；组内冻结模型权重不变。

1. 从已经验证的只读初态创建工作副本，检查 DB 完整性与逻辑初态指纹；工具目录也必须符合冻结版本。
2. 环境进程只获得运行必需文件与配置；模型入口没有通用 shell、任意文件读取或直连审计 SQL 能力。普通子进程隔离资源，但不能单靠子进程证明文件访问隔离，应通过实际挂载／OS 权限或容器边界验收。
3. 评测进程获得初末态和 verifier；Actor 不获得隐藏答案、评测输出、其他候选文件。verifier 本身是执行代码，也放在受限进程，避免改动正式快照。
4. 收尾先禁止新调用，确认在途执行停止，再使用 SQLite 一致备份机制保存快照。不能仅复制可能处于 WAL 写入中的主文件。
5. 冻结快照后计算哈希，评测读取副本；快照内容、真实最终答复和轨迹共同确定一次评测输入。
6. 记录环境逻辑时间、时区、相关时钟能力。无法控制真实时间的任务应单列，不假定所有服务都支持冻结时间。

幂等结算键至少包含轨迹／attempt、初末态 hash、trajectory hash、最终答复 hash、verifier hash、Judge 配置、rubric/reward 版本。相同数据库但答复不同的只读任务不能共享判分结果。

## 9. 上下文与观察

首版选择追加历史，单次长观察外置，不进行自动历史摘要。其价值是实现和验证简单，是否足够长程任务仍需测量。

每轮检查：`actual_input_tokens + reserved_output_tokens + safety_margin <= configured_context_limit`。输入计数包括系统提示、工具说明、聊天模板和已返回观察；无法精确计数的 API backend 记录估算方法和误差余量。GRPO backend 必须给出实际 token。

- 超限：停止新生成，保留轨迹、记录 context_limit，依可信终态评测。既不能自动记失败，也不能从统计中删除。
- 大返回：先保存本轨迹允许看到的完整原始观察，再提供确定性截取、总量、截断标志和 opaque observation_id。分页读取有大小上限，也计入上下文与成本。
- 分清“环境已返回”“存储中可读取”“已经送给模型”三个状态。模型没有读取外置部分，就不能认定模型已经见到其中的付款记录。
- 正常数据字段中的对象 ID 可保留以维持关联；密钥和认证令牌单独处理。凭证脱敏与隐藏答案隔离是两种不同机制。
- 如后续加入历史压缩，必须在采样时真实执行，保存每轮压缩前后映射和模型实际输入；训练不能拿事后生成的摘要替代原输入。

## 10. 证据记录

事件流追加写，原材料用 hash 引用。最少保存：

```text
event_id, parent_event_id, rollout_id, attempt_id, turn_id, seq
event_type, time_started, time_finished, visibility
raw_model_output_ref, parsed_action, parse_status
tool_name, arguments_ref, transport_status, mcp_is_error
raw_result_ref, visible_result_ref, truncation_metadata
commit_status, before_state_ref, after_state_ref, evidence_status
model_version, usage, latency, error_type
```

不同事件使用不同字段，不要求每次事件都有数据库 diff。MVP 至少对已识别的写操作／可能有副作用的操作保存前后审计证据；读写属性未知时不能默认无副作用，也不能只根据 GET/POST 判断。

三类证据视图：

| 视图 | 可放什么 | 用途 |
|---|---|---|
| model_visible | 实际送入模型的任务、工具说明、历史、观察 | 重建采样条件、评价当时是否有依据 |
| runtime_returned | 原始 MCP 返回、尚未展示的观察、调用状态 | 判断渲染是否丢信息；不自动算作模型已知 |
| audit_private | 初末 DB、写入差异、verifier 输出、诊断 | 确认客观后果；不回流给同次 Actor |

为每条 required 规则记录所需证据能否取得。缺少调用返回、分页、写入增量时应报 unknown，而不是由模型补出事实。MVP 不做通用 Python 数据流追踪；当前 MCP 单动作协议首先解决跨调用来源和模型可见性。

## 11. 结果评测、过程评测和奖励

### 11.1 结果评测

继续使用官方 SQL+LLM 路径。输入为原始任务、真实官方格式 trajectory、初态、终态和绑定 verifier。保存实际送入 Judge 的请求与原始响应、finish reason、解析状态、用量和版本；记录器不得添加独立参考标签或修改官方判定提示词。

优先做一项合同检查：只读任务的最终答复真的出现在官方 Judge 请求中。只把答复放进顶层 messages 不够。

RewardAdapter 建议：

| 条件 | reward_valid | reward |
|---|---:|---:|
| 任务／配置获准用于奖励，执行及取证有效，最终分类 complete | true | 1 |
| 同样有效，分类 incomplete，或经合同确认属于模型错误的 agent_error | true | 0 |
| server_error、judge_error、非法／截断输出、SQL 异常、终态不可信、评分依据冲突 | false | null |

服务返回 JSON 可解析不代表有效；输出截断必须独立检查。若原始分类与执行证据明显矛盾，标记待复核，不悄悄改成自己想要的成功／失败。首次有效判定按冻结策略保留，不通过多次重判挑选奖励。

`official_pass` 是指定官方配置通过；不自动等价于全面业务正确。已确认有漏检的任务配置回到上游评测修复／清洗流程。调试可继续运行，但不作为已验收奖励加入正式训练。

### 11.2 过程评测

复用旧方案的 required/quality/diagnostic 和 satisfied/violated/unknown/not_applicable，但规则类别、来源、适用条件、计分映射必须提前冻结。过程 Judge 看到行动之前的合法证据；审计器可核实行动后果，二者不能混用。

例如“必须先用工具 X 再用 Y”只有明确依赖依据时成立；“多查询一次”“出现异常”不能天然算违规。若初始指令已经提供所需事实，也不能强制多查一次来满足形式要求。

过程 Judge 先离线用于筛选和诊断，独立校准后再考虑 Q。J2 先看原始材料再看 J1，不共享先验结论；两者一致仍不能替代独立正确标签。

### 11.3 两个奖励实验必须分开

第一阶段：`R = S_official`，但只在 reward_valid=true 时有定义。正式训练需指定任务与评测配置已通过相应校准。这个基线优化官方终局通过，不能宣称已解决过程违规。

后续若要训练必须满足的过程要求，需另设已校准门控 `G_required`：`R = S_official × G_required × (1 + λQ)`。required 不通过不可被 Q 抵消；required 未知使该奖励配置无法有效结算。过程门控不能替代有缺陷的官方结果判分器。

Q 只使用已校准 quality 项；适用集合为空或有 unknown 时按冻结策略保留 null。若只缺 Q，先补评，仍缺则整组回退到相同基础奖励，并记录回退率；若基础／required 结算无效，则不能靠取消 Q 变有效。

小 λ 不能保证训练影响小，组内标准差归一化可能抵消尺度；归一化、KL、奖励权重应随实验配置冻结并做消融。全零／全一组仍保留统计，但没有组内任务奖励差异。

## 12. 三个阶段的合同

| 阶段 | 采样和保留 | 不允许的操作 |
|---|---|---|
| Teacher collect | 从干净初态生成；保留全部尝试；仅合格完整示范导入 SFT | 用私有 Judge 提示修复后冒充原次独立示范 |
| evaluate | 固定任务与采样次数；报告成功、失败、无效和预算停止 | 删除困难题、只统计成功运行、挑最好重跑 |
| GRPO rollout | 同一任务／初态／策略版本采 K 条，初始 K=4；保留有效失败 | 仅成功轨迹训练、混组／混权重、隐藏补采直到凑出成功 |

GRPO 导出每轮真实 `prompt_ids / response_ids / response_mask / old_logprobs`，以及 tokenizer、chat template、sampling 参数、policy version。环境和用户 token 不作为模型动作 token 计算策略损失；新生成的所有动作 token 是否计入由训练协议明确，不能只保留“正确代码”。

old_logprobs 可在采样时记录，或用完全相同的冻结行为策略对原 token 和原输入重算并校验；必须说明温度等采样变换与所用训练框架的概率合同。黑盒 Teacher 文本不具备成为当前 Actor on-policy rollout 的资格。

一组有基础奖励无效项：先补评同一终态。MVP 在仍无效时不将整组送入更新，保留该组全部记录及原因；不能偷偷以有效子集冒充固定 K。按模型行为、任务类型报告这类剔除，检查是否形成偏差。需要补采则以预先冻结的整组重采策略执行并标明新组。

SFT 导出同样保留原动作文本、工具返回和对应 mask；纯文本日志不足以证明 GRPO 导出正确，必须在所选训练框架进行一次真实组内 advantage 和更新验收。

## 13. 实施顺序与验收

以下是建议顺序，不是已经完成的工作，也不自动授权付费批量运行。

| 阶段 | 最小实现 | 验收依据 |
|---|---|---|
| P0 合同固定 | TaskSpec、动作协议、状态字段、运行配置、准入边界 | 正常／非法动作、结束和格式错可区分；最终答复正确进入官方输入 |
| P1 单轨迹闭环 | 一个 worker、MCP、事件、静止与快照、官方评测适配 | 从初态到终态可追溯；无手工补日志；同输入补评不重跑 Actor |
| P2 异常与隔离 | 写超时、服务退出、上下文超限、并发候选 | 无重复重发、无串库、快照一致；错误不被静默当成功 |
| P3 采集与评测 | 两个阶段入口、冻结数据划分、最小 SFT 导出 | 成功与失败均留痕；指标有明确分母；SFT 无隐藏证据 |
| P4 GRPO 接口 | 训练 Actor backend、真实 token/logprob、组装、一次更新 | 四候选初态和权重一致；环境 token mask 正确；失败参与，invalid 不当 0 |
| P5 过程质量 | 证据包、冻结规则、Judge 校准、筛选／Q 消融 | 正反例误放误拒、unknown、成本与成功率分别报告 |

开发试点可先选 3 个环境约 12 个任务，覆盖只读、单写、多对象／多步、长返回和相对时间。实际选题以可用资产为准，数量是预算建议，不是可靠性样本量结论。先使用脚本化模型输出验证基础合同，再用获授权的小批真实模型调用检验闭环。

关键验收用例：

- 两个工具调用一次输出：零业务执行，返回格式错误。
- 价格写入成功后另一次写超时：不重发成功写入；审计记录保留实际变化。
- 一个客户端超时后服务仍运行：未确认静止前不能冻结终态。
- 相同初态四条候选：其中一条写入，另外三条不受影响。
- 已有不完整初始化：即使 DB 文件存在也不能准入。
- 相同 DB、正确／错误最终答复：分别提交官方 Judge，输入及缓存键不同。
- 模型声明成功但数据库没改：不得从 finish 直接得 1。
- 中间 reward_type 为 incomplete、最终分类 complete：奖励读取最终有效结论。
- Judge 截断或 SQL 异常：reward=null；补评不修改原轨迹。
- 上下文或轮次耗尽：记录仍在评测分母，按真实结果结算。
- Q 缺分与基础 reward 无效：两种情况走不同组处理策略。
- token 导出：训练输入与真实采样输入逐 token 对齐，工具 token 不计算动作损失。

## 14. 产物与报告

建议目录（均为待实现）：

```text
harness_awm/                 # 共享运行代码
configs/harness/             # 冻结运行配置
runs/<run_id>/
  manifest.json
  episodes/<rollout_id>/<attempt_id>/
    initial.db, working.db, final.db
    events.jsonl, observations/, model_turns/
    trajectory.json, episode_result.json
    evaluations/<evaluation_id>/
  groups.jsonl
  exports/
  metrics.json
```

报告至少包括：固定任务数和候选数、有效成功／有效结算数、成功／全部预定候选数、invalid 比例及原因、任务／环境分层表现、context_limit 比例、观察截断与回读、token 和耗时、物理请求及重试、SFT 准入率、GRPO 无效组／同分组／Q 回退比例。success/全部候选仅表示观测到的完成比例，不能把 invalid 解释成已证实业务失败。

只对独立任务／实例层面的采样报告相应统计不确定性；同一任务四条候选和同一案例多次 Judge 调用不能当四个独立任务。

本次交付范围为附件审查、本地源码核对及设计文档。未修改官方源码、未实现上述模块、未运行新的环境实验、未产生训练效果结论。
