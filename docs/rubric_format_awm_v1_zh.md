# AWM 逐题验收 rubric 格式 v1（草案）

日期：2026-10-24。本轮完成格式设计和真实题目示例；尚未生成全部任务的 rubric，也未运行 MCP 正反例或 Judge。示例是可供审核的标准草案，不是已通过运行验收的任务证书。

## 1. 先确定交付形态

**每题一份 rubric，每条只判断一个可以独立说明的要求。人读表格与程序读 JSON 表达同一套标准。**

本地 `data/awm-dde80a0/gen_tasks.jsonl` 有 1,000 条环境记录，每条包含 10 道题，共 10,000 题。以下任务编号采用数组下标，从 0 开始；`e_commerce_33:9` 是该环境第 10 题。该数量是原始任务数量，不是已验收数量。

本轮查看了购物、任务管理、资助管理、统计查询等题面，并重点核对了 `e_commerce_33`、`payments_1` 的公开接口、数据库 schema 和环境实现。题库同时包含写入、查询、排序选择、批量操作、时间范围和输出格式要求，因此不能只用末态数据库或最后一句答复验收所有题。

建议采用下面的表头。五个核心字段完全保留，并增加编号和评价对象，便于定位与证据关联：

| 编号 | 评价标准 | 依据的题目原文或规则 | 必要条件还是质量偏好 | 评价对象 | 需要的证据 | 判定方式 |
|---|---|---|---|---|---|---|
| R01 | 具体对象在指定时点必须满足的一个条件 | 原文逐字引用＋文件／条目定位；有推导时说明推导 | `required` 必要条件／`quality` 质量偏好 | `outcome` 结果／`process` 过程／`answer` 答复 | 具体记录、字段、调用事件或输出内容，以及取证时点 | 检查方法＋满足／违反／无法判断条件；必要时写不适用条件 |

**“必要或偏好”和“结果、过程或答复”是两个维度。**例如题目明确说“先检索再发布”，过程要求也是必要条件；“排版更容易阅读”即使评价最终答复，也可能只是质量偏好。

## 2. 五个核心字段如何填写

| 核心字段 | 必须写清楚 | 不能只写 |
|---|---|---|
| 评价标准 `criterion` | 对象、范围、属性／动作、期望、必要时的时点。例如“同一条新评论的 rating 为 5” | “正确完成”“操作合理”“结果准确” |
| 依据 `sources[]` | 来源类型、文件、任务／接口选择器、连续原文引用；规范化或推导说明 | “符合用户意图”“参考答案就是这样” |
| 必要／偏好 `requirement_level` | `required` 决定是否验收；`quality` 单独反映质量，不抵消必要条件 | 让 Judge 看完轨迹后决定硬软性质 |
| 证据 `evidence[]` | 来源、对象绑定、字段、完整性要求、初态／末态／动作前时点；明确必需材料和可替代材料 | “看数据库”“看完整轨迹” |
| 判定 `decision` | 用代码、语义 Judge 还是组合；精确比较谓词、允许变体、缺证据与异常处理 | “LLM 综合打分 0–10” |

来源允许：

- `task`：题面要求，包括限定词、数值、时间、操作顺序和输出要求。
- `public_contract`：公开工具说明或已声明环境规则，例如“当前用户”“日期上界不包含”。字段定义本身不意味着题目要求设置该字段。
- `evaluation_policy`：额外提出的统一质量政策，必须单独写明版本和适用范围。没有用户或既有规则依据时保持候选，不能悄悄升级为必要条件。

环境代码和 schema 用于核实可取证字段、工具行为及契约冲突；实现细节不能自动覆盖题意。官方 verifier 用于后续覆盖比较和差异分析，不用它反向定义独立 rubric。本文三组示例未以官方 verifier 的判断条件作为依据。

## 3. 每题外层信息与每条补充字段

除表格外，每题保存：

| 字段 | 用途 |
|---|---|
| `schema_version`, `rubric_version`, `status` | 格式版本、规则版本、`draft / frozen / superseded` |
| `task_id`, `scenario`, `native_task_id`, `task_text` | 稳定定位题目及完整原文；不只保存中文转述 |
| `dataset_version`, `task_text_sha256` | 防止题面变化后沿用旧规则 |
| `initial_state_hash`, `environment_code_hash`, `tool_schema_hash` | 正式使用时绑定初态、实现、接口；设计阶段可以为空，冻结时不可缺必需绑定 |
| `time_context`（适用时） | 运行锚点、时区、日期／时间戳语义、窗口边界；不能默认用写 rubric 当天的时间 |
| `context_facts` | 当前用户、默认对象等已核实事实及来源；不是凭空增加的输出义务 |
| `clause_coverage` | 每个题面要求映射到哪些条目；背景文字注明理由 |
| `unresolved` | 真正影响正确性且尚不能消解的范围、时点、契约冲突 |
| `aggregation` | 固定的汇总政策；独立 rubric 结论与官方 SQL+LLM 结论分开保存 |

每条再保存 `id`、`aspect`、`target`、`applies_when`、`depends_on`、`validation`。同一评论的产品、评分、标题、正文必须绑定**同一个对象**，不能分别找四条满足各自字段的记录凑成成功。

`decision` 至少包含 `method`、`satisfied_when`、`violated_when`、`unknown_when`、`not_applicable_when`。需要数值或集合比较时再加 `operator`、`expected_value`、`normalization`；实现后加真实 `check_ref`。不要把不存在的检查器路径写成已经实现。

现有 [LLM 清洗方案](llm_driven_cleaning_plan_zh.md) 的 `kind` 可以映射为：`required_outcome` → required/outcome，`required_answer` → required/answer。`context_fact` 保存在外层；`contract_precondition` 只有形成有依据的行动约束时才转为 required/process，否则作为适用条件或环境事实。现有 `source` 扩展为 `sources[]`，`evidence_inputs` 扩展为带字段和时点的 `evidence[]`。

## 4. 统一判定与汇总

| 条目状态 | 含义 | 例子 |
|---|---|---|
| `satisfied` | 有充分证据证明满足 | 指定新评论的 rating 确实为 5 |
| `violated` | 有充分证据证明违反 | 同一评论 rating 为 4；或完整快照证明根本没有发布评论 |
| `unknown` | 必需证据缺失、范围未定、材料冲突或检查失败 | 末态快照缺失；不能确定“最近90天”的锚点 |
| `not_applicable` | 预先声明的条件确定不成立 | 只在发生可恢复错误时适用的恢复质量规则，本次无此错误 |

无条件必要条目不能标为不适用；适用性无法确定应标 `unknown`。日志没有记录和记录完整但没有发生，是不同情况。`check_error` 单独记录错误原因并使受影响项 `unknown`，不能解释为业务失败。

汇总顺序：

1. 任务绑定、执行真实性或证据完整性存在使本次评价不可信的基础问题，整题为 `invalid`，保存已有局部发现但不出有效奖惩。
2. 在证据有效的前提下，任一必要项有可靠的 `violated`，整题为 `fail`；其他未知项继续保留，不能宣称全部已查清。
3. 没有必要项违反，但仍有必要项 `unknown`，整题为 `review`。
4. 所有适用必要项均为 `satisfied` 才为 `pass`。必要项为空或全部不适用时，除非有预先核实的特殊依据，否则进入 `review`，不能空集自动通过。
5. 质量偏好独立报告，v1 不设置加权总分。质量未知不改变已确认的任务成功，也不能靠质量加分补偿必要项失败。

这是**一次执行的 rubric 验收状态**，不是题库清洗的 `keep/reject/quarantine`。Agent 做错一道题，不代表题目应被删除。正式奖励仍按仓库现有 SQL+LLM 协议结算；本文不修改 reward 实现。

## 5. 真实题目示例 A：查找商品并发布评论

来源：`gen_tasks.jsonl` 第 1 行，`scenario=e_commerce_33; tasks[9]`。

> Search for 'LEGO Star Wars Millennium Falcon', open the product with the highest number of customer reviews, and post a 5-star review with the title 'Amazing build quality' and body text 'Took a full weekend to build and was worth every minute.'

接口依据：`gen_spec.jsonl` 第 1 行，`search_products`、`get_product_by_id`、`create_product_review`。创建评论接口明确为当前用户新建记录，返回的 `user_id` 说明为 `Current user ID (implicit 1).`。

下表的“目标商品”统一指匹配该搜索范围、在发布前评论数最大的商品；并列最大时，题面未指定进一步排序，接受任一并列者。创建评论可能改变计数，所以不能用操作后的排名倒推选择正确性。

| 编号 | 评价标准 | 依据的题目原文或规则 | 必要／偏好 | 评价对象 | 需要的证据 | 判定方式 |
|---|---|---|---|---|---|---|
| R01 | 实际搜索了指定商品范围 | “Search for 'LEGO Star Wars Millennium Falcon'” | 必要 | 过程 | 搜索请求、真实返回、事件顺序 | 检查实际检索覆盖题述商品范围；只口头声称搜索不满足。允许有证据的等价检索，不强制某个工具名 |
| R02 | 选中商品属于该搜索范围 | 同上；接口搜索字段为商品标题和描述 | 必要 | 结果 | 选中 product_id、商品字段、检索范围定义 | 按已核实搜索语义检查成员关系；商品不匹配则违反 |
| R03 | 选中商品的评论数是候选范围内最大值 | “with the highest number of customer reviews” | 必要 | 结果 | 发布前完整候选及 `product_aggregates.review_count`；覆盖全部分页或等价完整证据 | 对完整范围计算最大值；同分任选。不完整的一页不足以证明全局最高 |
| R04 | 发布前打开了选中商品详情 | “open the product ... and post” | 必要 | 过程 | 详情读取事件、返回的 product_id、发布事件 | 验证详情对象相同且发生在发布前；允许等价详情接口，不强制相邻两步或其他无依据顺序 |
| R05 | 为目标商品实际新建了当前用户的评论 | “post a ... review”；接口：“Create a new product_reviews entry for the current user for a product.” | 必要 | 结果 | 初末态 `product_reviews`、真实创建返回、目标 product_id、当前用户规则 | 绑定本次新建 review_id，初态不存在且末态属于目标商品和当前用户；历史已有评论不能代替本次发布 |
| R06 | 同一新评论评分为 5 | “5-star review” | 必要 | 结果 | R05 的同一评论 `rating` | 整数等于 5；写成 4 则违反 |
| R07 | 同一新评论标题为指定文本 | “title 'Amazing build quality'” | 必要 | 结果 | 同一评论 `title` | 按解析后的字符串精确比较，不任意改写 |
| R08 | 同一新评论正文为指定文本 | “body text 'Took a full weekend to build and was worth every minute.'” | 必要 | 结果 | 同一评论 `body` | 精确等于题述正文；空值、改字或漏句均违反 |

R05 经完整证据确认失败时，R06–R08 的要求也未完成；若 R05 只是对象绑定或快照缺失，则下游为 unknown。多个新评论存在时，绑定必须结合真实创建事件和完整要求，不能任意选择一条掩盖冲突；题面未要求“全库恰好一条评论”，不能用全库计数代替验收。

该题未要求最终答复重复完整评论内容，不额外增加“必须在最后一句逐字复述”的硬条件。

质量偏好可以为空。若后续采用统一答复质量政策，可另列“简要说明发布到哪个商品及评分”，来源明确写政策而非题面；它不能决定本题成功与否。本文未将该候选偏好激活。

对应的 [JSON 单条样例](rubrics/item_example_e_commerce_33_9.json) 展示 R08 的完整结构；它明确标注为片段，R05 等依赖需要在整题 rubric 中补齐，不能直接拿它验收整题。

## 6. 真实题目示例 B：排序选品与购物车

来源：`gen_tasks.jsonl` 第 1 行，`scenario=e_commerce_33; tasks[0]`。

> Search for 'wireless noise cancelling headphones', sort results by average customer rating, and add the top-rated item under $200 to my cart in quantity 1.

| 编号 | 评价标准 | 依据的题目原文或规则 | 必要／偏好 | 评价对象 | 需要的证据 | 判定方式 |
|---|---|---|---|---|---|---|
| B01 | 搜索指定耳机范围 | “Search for 'wireless noise cancelling headphones'” | 必要 | 过程 | 查询及真实返回 | 验证实际检索范围；允许可核实的等价检索 |
| B02 | 依据平均客户评分排序／比较结果 | “sort results by average customer rating” | 必要 | 过程 | 排序请求及返回，或可审计的等价本地排序记录 | 不能用评论数量、销量替代评分；不强制唯一调用路线 |
| B03 | 加入的商品属于所搜索的耳机范围 | “add the top-rated item”及搜索限定 | 必要 | 结果 | 商品字段、offer 到 product 关联 | 检查最终加入对象属于限定候选集 |
| B04 | 所选报价为美元且单价严格小于 200 | “under $200” | 必要 | 结果 | 所选 `product_offers.price/currency`、操作时有效报价 | 比较 USD 且 price < 200；200.00 不满足，不以搜索参数代替实际价格证据 |
| B05 | 所选商品在符合预算的候选中平均评分最高 | “the top-rated item under $200” | 必要 | 结果 | 完整候选、有效报价、`average_rating`、选择时点 | 先限定预算可满足的商品，再比较评分最大值；接受并列最高者 |
| B06 | 目标商品实际位于当前用户的有效购物车 | “add ... to my cart”；公开规则为 user_id=1 的 active cart | 必要 | 结果 | 初末态 carts、cart_items、product_offers，以及真实写操作 | 验证正确用户、购物车、报价与商品关联及本次操作效果；一句“已添加”不足 |
| B07 | 按题意完成数量 1 的加入操作 | “in quantity 1”；接口数量与合并语义 | 必要 | 结果 | 初态数量、写操作参数／返回、末态数量 | 目标原先不存在时，核验新增数量为 1；原先存在时先核实增加数量还是设为总数量的解释，再冻结比较谓词 |

这里必须保留两个实际实现风险：

- `gen_envs.jsonl` 中 `scenario=e_commerce_33` 的 `search_products` 先执行 `offset/limit`，随后才对本页按评分排序。它还可能保留 `lowest_active_offer=null` 的商品。因此“取第一页第一名”或“传 max_price=200”都不能单独证明 B04/B05。
- `add_item_to_cart` 对已有商品，`merge_if_exists=true` 增量加入，false 则覆盖数量。题面、公开契约与绑定初态共同决定 B07 的验收口径；不能不看初态就一律要求末态 quantity=1，也不能擅自一律要求 delta=1。若该初态确有歧义，保留 unresolved，不能冻结整题。

本例没有对真实初态执行查询验收，因此没有填写唯一商品 ID 或宣称 B07 已解决。库存、任意额外分类筛选也不能未经依据就用来缩小“最高评分”的候选范围。

## 7. 真实题目示例 C：时间范围统计与答复

来源：`gen_tasks.jsonl` 第 27 行，`scenario=payments_1; tasks[8]`。

> Retrieve my transaction history for the past 90 days filtered to only show 'Goods and Services' payments received in EUR and summarize the total EUR amount received and the count of such transactions.

接口来源：`gen_spec.jsonl` 第 27 行的 `list_transactions`、`summarize_transactions`。两者将日期下界定义为 inclusive，上界定义为 exclusive；分类枚举为 `GoodsAndServices`，收款方向为 `received`。当前用户规则还需与任务实例的公开身份信息绑定。

| 编号 | 评价标准 | 依据的题目原文或规则 | 必要／偏好 | 评价对象 | 需要的证据 | 判定方式 |
|---|---|---|---|---|---|---|
| C01 | 取得的历史记录属于当前用户 | “my transaction history” | 必要 | 结果 | 公开用户身份、完整检索结果、transactions.user_id | 检查正确用户范围 |
| C02 | 历史记录采用确定的过去 90 天窗口 | “for the past 90 days”；日期边界契约 | 必要 | 结果 | 运行锚点 T、时区、时间解释、created_at、筛选记录 | 若已核实采用连续 90 天，则比较 [T−90天,T)；不得自行换成本月／最近三个月；时间定义未定则 unknown |
| C03 | 记录属于商品与服务类支付 | “'Goods and Services' payments” | 必要 | 结果 | category、type 及公开业务定义 | 分类映射为 GoodsAndServices；payment 的 type 范围按核实定义冻结，不能靠标签猜测 |
| C04 | 记录方向为收款 | “received” | 必要 | 结果 | transactions.direction | 等于 received；排除 sent/internal |
| C05 | 记录币种为 EUR | “in EUR” | 必要 | 结果 | transactions.currency_code | 等于 EUR；不可混入其他币种金额 |
| C06 | 检索结果完整覆盖所有同时满足 C01–C05 的记录 | “Retrieve my transaction history ... filtered to only show” | 必要 | 结果 | 独立范围查询、完整工具结果／分页证据 | 比较成员集合及重复情况，不能只检查返回记录都合格而漏检缺失记录 |
| C07 | 提供正确的 EUR 收款总额 | “summarize the total EUR amount received” | 必要 | 答复 | C06 的完整记录及 amount、实际对用户输出 | 对同一记录集合独立求和，再对照输出金额和币种；明确货币精度与舍入规则 |
| C08 | 提供正确的交易笔数 | “the count of such transactions” | 必要 | 答复 | 同一记录集合、实际对用户输出 | 对同一范围独立计数并核对输出；不能用接口返回数组的截断长度 |

冻结前需确定：运行时间与时区、payments 的类型语义、输出呈现位置。若工具结果在协议中直接作为用户可见交付，允许该位置承载结果；否则统计摘要需进入最终答复。题面未要求 JSON、表格或逐字固定措辞，不擅自增加这些限制。

题面没有明确 `status=completed`，不能仅因接口示例出现 completed 就加此过滤。公开业务定义若能消解“received”的状态含义，记录来源；否则只将会改变正确性的分歧保留为 unresolved。`summarize_transactions` 没有 type 过滤参数，不能未经核对就认为它与独立定义的支付集合一致。

完整证据证明确实没有匹配记录时，总额 0、笔数 0 可以满足；工具异常或缺少记录则不是 0。空集时还需要核查实际查询范围，避免错误过滤恰巧也得到空集而被误判通过。

## 8. rubric 本身如何验收

在逐题批量生成之前，先用以上结构检查每份标准：

1. **来源可追溯**：原文引用确实存在，且支持该条标准。示例值、参考解法和实现偶然行为不自动成为要求。
2. **题意覆盖完整**：动作、对象、字段、限定范围、批量、时间、顺序、输出都映射到条目；不能只保留容易检查的字段。
3. **条件可独立说明**：评分、标题、正文分别验收；每条仍必须绑定同一目标对象。多字段共同定义一个集合时，保存共同绑定与依赖关系。
4. **证据能够取得**：明确依赖初末态、完整分页、动作前信息还是用户可见输出。末态无法证明中途操作顺序；私有数据库事实不能冒充 Actor 当时已经知道的信息。
5. **判定边界明确**：并列、空集、已有对象、新 ID、数值精度、相对日期、合法替代路径和缺证据都有处理方式。
6. **规则经过案例检验**：至少核实一条完整正例；每个可独立违反的必要条件有针对性反例；验证适用的合法替代正例和缺证据案例。不可独立干预时记录真实接口限制，而不是编造反例。
7. **冻结与评测分开**：先用开发案例修正规则，再冻结正式评测版本；修改规则后统一重评受影响执行。不能看完某个待评 Agent 的表现后单独为它改标准。

`validation` 保存案例引用、核验结果和限制；JSON 可解析、SQL 能运行、两个 Judge 一致，都不能代替这些语义检查。无未决实质歧义、必要绑定齐全且检查器验证完成后，才把 status 从 draft 改为 frozen。

## 9. 本轮检查边界与源码定位

- 已用程序核对原始题数，读取上述题面、公开接口、schema、相关环境函数；新建了本文与一个 JSON 条目片段。
- 已知数据库文件存在，但本轮 `mode=ro` 打开导入快照失败，未取得初态查询证据；不将其解释为任务或数据库缺陷。本轮结论不依赖实际初态值。
- 未启动 MCP、未构造真实正反例、未运行官方 Judge、未请求付费模型 API；示例尚未达到 frozen。
- `gen_tasks.jsonl`：购物例在第 1 行，统计例在第 27 行。
- `gen_spec.jsonl`：对应环境也分别位于第 1、27 行；记录应始终按 scenario 关联，不能假设各文件行序一致。
- `gen_db.jsonl`：e_commerce_33 在第 1 行，payments_1 在第 23 行；用于核实上述字段。
- `gen_envs.jsonl`：e_commerce_33 在第 844 行，payments_1 在第 24 行；相关实现存储于 `full_code` 字符串。
- 现有生成提示词在 `scripts/two_layer/prompts.py`；现有过程评价边界在 `docs/harness_design_awm_v1_zh.md` 第 11.2 节。

下一步逐题填充时，以本格式产出候选 rubric，先消解来源和证据问题，再实现检查与案例验证；不要直接把草案当成奖励标准。
