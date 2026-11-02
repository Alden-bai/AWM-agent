# 五字段 rubric：规范化输入、义务清单与语义案例检查

本实现入口为 `scripts/rubric_qwen_v11.py`，纯离线逻辑为 `scripts/rubric_workflow.py`。每道题最终仍是 `task_id / rubric / coverage / unresolved`；每条保留 `id` 和五个业务字段 `criterion / sources / requirement_level / evidence / decision`。计划和测试结果另存，不改变交付模板。

## 输入规范化

从 v10 冻结的相同十题原始输入构建：

- `task`：完整题面。
- `public_contracts`：解析后的公开接口契约，保留所有非 example/examples 属性。
- `non_normative_examples`：集中存放接口示例，并保留源 ID 和 JSON Pointer；不能当作实际状态或默认值。
- `schemas`：完整原始 DDL；不包含数据库行。
- `citation_catalog`：题面、契约 description/summary 以及 DDL 行的原文片段和来源 ID。JSON 中转义字符按原始序列化保留。
- `tool_inventory / evidence_context / quality_policy`：保持原输入语义，不用未提供接口契约推断能力不存在。

离线审计文件记录原文哈希及引文起止位置。重新插回示例后必须等于原契约，证明未丢弃其他字段。原始输入另存，引用最终仍对原始输入校验。该版本优先消除来源角色混淆，**并非输入压缩实验**；引用目录和分离示例会增加输入长度。

## 义务与案例提取

每题先调用一次模型，输出单独的 `obligations / ambiguities / cases`：

- 每项义务记录原文、要求、对象绑定、时间范围、来源引用。
- 歧义必须指出影响哪些义务，以及需要什么信息消解；没有运行轨迹不等于题意歧义。
- 生成前冻结正例、逐义务反例、合法替代路径和缺证据案例。每个义务必须有反例覆盖。

程序检查引文、类型、ID、案例种类、反例覆盖及预期值。结构不合格时停止该题后续收费步骤，保留原始返回；不会自动修复或套用历史草稿。

第二次调用生成 rubric。输入只包含规范化来源、义务和歧义，**不包含案例及其预期结果，也不包含历史审查清单或旧 rubric**。义务是待核对分析，不具有超出公开来源的权威。`coverage.interpretation` 用 `[O01]` 标记义务映射；这些标记不等于语义覆盖已被证明。

## 语义案例检查

第三次调用获得来源、义务、案例和新 rubric，重新核对义务，再按 rubric 实际 G/P 推演案例，输出逐项结果与反例发现。判定类型为 satisfied / violated / unknown / ambiguous。案例本身有误则标记 case_valid=false。

本轮是**同一模型另一次独立请求的语义模拟**，不是独立模型裁判，也不是执行真实环境或可执行业务检查器。案例预期与裁判可能存在相关错误，因此还需要对照冻结的原审查参考进行来源复核。历史参考不进入任何模型请求。

程序只在每个义务、每个案例恰好出现一次、引用有效、所有案例有效且预期一致、没有遗漏/歧义/发现时，标记 `semantic_candidate`。缺项、重复、歧义、无效案例、格式错误都不能记作通过。该标记只表示模型建议保留，最终独立审查另计。

G 应描述足够判断的证据和语义；动作、正确关联、格式和业务成功属于 P。特别检查“完整证据证明没做”不能变成 unknown，以及合法本地筛选/排序不能因为未使用指定参数而被误拒。

## 可复现运行与预算

```bash
agent-world-model/.venv/bin/python -m unittest discover -s tests -p 'test_rubric_workflow.py' -v
agent-world-model/.venv/bin/python scripts/rubric_qwen_v11.py prepare
agent-world-model/.venv/bin/python scripts/rubric_qwen_v11.py run
agent-world-model/.venv/bin/python scripts/rubric_qwen_v11.py summarize
```

`prepare` 和 `summarize` 不调用模型。`run` 是收费步骤，只用于本轮明确授权的十题。已保存响应复用；已预留但响应未知的请求禁止重试。已准备目录不可覆盖。

本轮沿用 Qwen3.5-Flash、temperature=0、JSON 模式、thinking_budget=16384 和每步最多 8000 回答 tokens。输入估算、思考预算、回答预算及余量之和必须小于 128000。最多两请求并发、总计 30 请求；操作方设置 600000 token 保护上限，不是用户指定的总预算。每步请求均有不可变依赖哈希，记入已有共享账本，历史未知预留继续占用。没有自动修复、重试或全量生成。

原始返回、结构校验、模型语义检查、独立来源复核分开保存。比较对象是此前相同十题的 v10 思考版，也列出非思考版；该开发集已经多次查看，三个变更一起引入，不能声称单项因果效果或未见题泛化。
