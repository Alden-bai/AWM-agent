# v13：全部使用Qwen3.5-Flash，最多两轮修订

入口为 `scripts/rubric_qwen_v13.py`。使用原来的10道题；原文候选提取和五字段装配复用 `scripts/rubric_candidates.py`，不改写v12冻结代码、结果或评分口径。

## 验收与修订

每条最终rubric仍为 `criterion / sources / requirement_level / evidence / decision`，另带定位id。先通过格式、候选双向映射及原文引用检查，再交给Qwen独立审查。

五维为：来源支持、题意覆盖、硬软分类、证据设计、判定逻辑。每维必须为整数0/1/2并提供具体理由。**合法五维评分没有0分即通过**，包括全1分；任一0分则当前版本不通过。未完成、截断、漏维度或格式错误的审查不能默认为通过。

初稿后最多修订两轮；每轮返回完整新版本。生成模型收到本题完整公开来源、上一版中间输出与上一轮审查意见，须核实反馈是否符合原题。结构/映射错误使用程序反馈修订，也消耗同一个两轮额度。通过后立即停止，保留首次通过的版本，不再花费调用改写它。达到修订上限仍有0分则保留题目及失败版本，不交付为通过版。

独立静态语义复核不作为第三道验收门槛。本轮通过代表满足这套Qwen审查规则，并不等于经过运行验证或人工标注的质量准确率。同一模型生成和审查仍可能重复同一种误解。

## 每次审查都是全新上下文

每题每轮都是单独的API请求；消息列表严格只有两条：system审查指令、user中的本题资料和当前rubric。

审查请求不包含生成模型的reasoning_content、此前会话、上一版rubric、历史评分或修订意见。旧意见仅给修订生成器；新审查员看不到。审查payload由明确字段构造，汇总阶段逐请求重建核对，测试也覆盖修订后仍不泄露旧反馈。

## 成本与执行边界

- 全部使用已配置的Qwen3.5-Flash，生成/审查均temperature=0、thinking_budget=16384、max_tokens=10000；没有DeepSeek请求。
- 最多60次调用，两请求并发；失败网络请求不自动重发，用量未知时保留预算占用。
- 每次发送前，输入保守估算+输出及思考预留+余量必须严格小于128000。操作方另设150万token保护上限，该上限不是用户指定预算。
- 同一共享SQLite账本累计记账。历史未知预留不会因新试跑而清零；本轮只按实际报告的usage结算已知用量。
- 按本轮核验的Qwen北京128k内普通输入¥0.2/百万、输出¥2/百万估算费用，输出已包含思考，不重复累计；未核验缓存折扣和实际账单。价格来源：https://help.aliyun.com/zh/model-studio/qwen3-5-flash。

主指标定义：

1. 首次通过率：初稿通过所有程序检查且首次五维审查无0分的题数÷10。
2. 最终通过率：初稿或至多两轮修订后通过的题数÷10。
3. 每份通过rubric的费用：**本轮全部生成、审查、失败及修订的费用÷最终通过题数**。不能只算成功题的调用；通过数为0时该指标无定义。未知用量单独报告，不当成免费请求。

## 命令

```bash
agent-world-model/.venv/bin/python -m unittest discover -s tests -p 'test_rubric_qwen_v13.py' -v
agent-world-model/.venv/bin/python scripts/rubric_qwen_v13.py prepare
agent-world-model/.venv/bin/python scripts/rubric_qwen_v13.py run
agent-world-model/.venv/bin/python scripts/rubric_qwen_v13.py summarize
agent-world-model/.venv/bin/python scripts/report_rubric_qwen_v13.py
```

只有run会联网收费。prepare冻结输入、提示、代码哈希和授权范围；已有结果不覆盖，完整响应可按同请求身份复用。summarize和report为离线核验及报告生成。最终通过版位于本轮 `accepted/` 目录；所有轮次、评分与失败证据位于 `tasks/`。

## 本轮结果

首次通过3/10，最终通过5/10。26次API调用（18次生成、8次审查），338673 tokens，估算费用¥0.3919992，每份通过版¥0.07839984，含所有失败和修订成本。本轮未知用量为0，全部原题保留。

修订新增通过2题；其中一题有效评分为2/1/2/2/1，按无0规则通过。剩余2题用完修订额度仍结构失败，3题审查输出无效（额外字段或错误候选ID）。8次修订调用全部来自结构反馈；含0的两份审查同时格式无效，未触发实际语义修订。因此语义反馈闭环有单元测试验证，但本批API样本尚未验证其效果。

每次审查的实际请求均已核对为全新system+user，不含旧评分或修订意见。最大单次保守预留64330，小于128000。报告见 `runs/rubric_qwen_v13_10_20261024/report_zh.md`，5份通过版汇总见同目录 `accepted_rubrics_zh.md`。
