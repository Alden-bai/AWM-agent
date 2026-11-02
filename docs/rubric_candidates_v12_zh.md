# v12：代码候选、Qwen编译、DeepSeek独立五维评分

入口：`scripts/rubric_qwen_deepseek_v12.py`。纯离线提取/装配/评分门槛：`scripts/rubric_candidates.py`。最终交付继续是 `task_id / rubric / coverage / unresolved`；每条除定位 `id` 外只有五个业务字段：`criterion / sources / requirement_level / evidence / decision`。

## 1. 候选由代码提取并绑定原文

按标点和连接词分段，保护引号、括号/标签数组及千分位数字。每段有稳定候选ID、source_id、start/end、quote及量词/条件/时间/数量的词法提示。起止位置使用Python Unicode字符索引。重复文字保留不同位置；所有非空白字符必须被覆盖。

候选只是带位置的文字片段，并不声称是完整的语义约束。一段可能包含多个义务，一项义务也可能跨段，因此完整题面始终提供给两个模型。候选不能自动保证原文支持模型的解释。

接口的非example属性和完整DDL保留。description/summary在输入视图中带引用ID。示例隔离在本地审计文件，不进入模型请求；把引用包装还原并插回示例后，必须等于原始接口结构。引用注册表保存原始序列化文本中的连续片段、起止位置和来源哈希。

## 2. Qwen筛选并编写业务内容

一次独立请求同时输出筛选表和业务条目，减少收费阶段：

- 每个候选必须恰好有一个去向：keep / merge / background / ambiguous。
- 除背景外，每个候选必须映射到条目；候选到条目与条目到候选的双向映射必须一致。
- ambiguous保留义务，并关联unresolved；不能因不确定而删除义务或降为质量偏好。
- 每个条目至少引用一个题面候选，公开契约只作必要解释，不能创造额外任务要求。
- required是不可缺少的任务要求；quality须有明确可选偏好或质量政策来源。允许所有条目均为required，不强行创造quality。

Qwen中间产物包含requirement/gate/predicate、证据、方法及候选/契约ID。它不是伪装成原始五字段结果的“修复版”。代码把原文引用、固定G/P三值判定和coverage装配成最终五字段。业务语义不改写。原始中间输出、最终rubric和装配哈希分别保存。

只有合法原文绑定与完整候选处理能进入装配。伪造ID、重复/遗漏候选、关联不一致、截断或不完整JSON均保留失败证据，不自动清洗、不重试。

## 3. DeepSeek Flash五维评分

使用已配置的官方 `deepseek-flash`，官方在2026-10-24列出的版本为DeepSeek-V4.1-Flash-1017。每题单独的system+user请求，看到完整题面、公开来源、候选筛选表及最终rubric。不会收到Qwen思考内容、历史草稿、历史评分、既有审查参考或逐题反馈。

维度固定为：

| 维度 | 0分 | 1分 | 2分 |
|---|---|---|---|
| source_support 来源支持 | 无依据要求或明显曲解 | 解释边界需确认 | 每条有明确来源支持 |
| coverage 题意覆盖 | 遗漏必要动作/范围/条件/交付 | 覆盖关系不清 | 完整覆盖且去重未丢义务 |
| requirement_classification 硬软分类 | 硬要求降级或偏好无依据升级 | 分类依据不清 | 分类有来源且允许无quality |
| evidence_design 证据设计 | 依赖不存在字段或无法取证 | 范围/时点/关联需补充 | 可取得且足够具体 |
| decision_logic 判定逻辑 | 存在误收、误拒或失败转unknown | 边界仍需澄清 | 对象/集合/时点/三值逻辑一致 |

每维必须有理由、定位到条目/候选的发现和边界反例。分数必须是真正整数0/1/2；布尔值、漏维度、重复维度、错误引用或无理由不能通过验证。

- 有效评分中任一0分：`rejected`，淘汰**该版rubric**。
- 没有0分但有1分：`review`，待复核。
- 全部2分：`eligible`，候选可用，仍不是运行正确率认证。
- 评分无效/不完整：`review`或明确生成失败，不能默认为通过。

程序不使用总分抵消零分，不修改或删除原题。`retention_manifest.json`保存每个版本的去向和原题引用。独立来源复核另外保存，与DeepSeek评分分开报告。

## 4. 本轮试跑与成本边界

同一历史10题；最多10次Qwen及10次DeepSeek请求，两请求并发。Qwen沿用temperature=0、thinking_budget=16384，回答上限10000；DeepSeek使用思考模式high，max_tokens=16384（包含生成的思考与回答）。两者均在发送前检查保守输入字节估算加输出/思考预算及余量严格小于128000。

本轮操作方保护上限450000 tokens，不是用户指定总预算。沿用共享账本，历史未知预留保留；不自动修复、重试或推广全量。结构失败的题不继续派发审查。已有完整响应可复用，在途或用量未知请求禁止自动重发。

费用使用实际usage，分别统计生成与审查。DeepSeek按缓存命中/未命中及实际响应时段计价；本轮周六为空闲时段。价格来源：[DeepSeek官方人民币价目](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Qwen官方北京价格](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)。估算不是账单，不包含Codex开发/来源复核用量。

与v10思考及v11比较时，费用包含本轮两个阶段，格式改进区分“代码装配”与“模型原始输出”；质量采用相同静态来源审查口径。旧版没有同一DeepSeek五维评分，不能把新评分直接当作旧版到新版的准确率提升。数据是已知开发集，未测未见题泛化，也未执行实际任务。

## 5. 命令

```bash
agent-world-model/.venv/bin/python -m unittest discover -s tests -p 'test_rubric_candidates.py' -v
agent-world-model/.venv/bin/python scripts/rubric_qwen_deepseek_v12.py prepare
agent-world-model/.venv/bin/python scripts/rubric_qwen_deepseek_v12.py run
agent-world-model/.venv/bin/python scripts/rubric_qwen_deepseek_v12.py summarize
agent-world-model/.venv/bin/python scripts/report_rubric_qwen_deepseek_v12.py
```

prepare/summarize离线，run收费且仅限本轮授权十题。冻结目录存在时prepare不会覆盖；原始请求和原题保留。

report脚本为本轮离线比较入口，需要已经完成的独立来源复核和审查理由复核文件，不发模型请求。

## 6. 本轮实测

10次Qwen请求、8次DeepSeek请求。43个候选进入筛选，8份通过结构与来源绑定并装配；2份映射不一致被阻断。DeepSeek有6份完整五维评分，均至少一个0分；1份生成达到16384上限截断，1份连接失败且usage未知。没有可用版通过，原题全部保留。

已确认用量247533 tokens、估算¥0.506137，另保留40874 token未知用量及约¥0.090026保守费用预留。最大单次保守预留59928，低于128000。本轮没有付费重试。独立来源复核仍为0/10暂可保留；评分模型也存在错误解释，不能把它的淘汰结论或修复建议当作真值。

逐题五维分数、与v10/v11比较、失败原因和费用在 `runs/rubric_qwen_deepseek_v12_10_20261024/report_zh.md`；原始/装配产物及版本状态均保留。
