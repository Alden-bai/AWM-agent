# Qwen3.5-Flash

在项目根目录 `.env` 中填写 `DASHSCOPE_API_KEY=你的Key`。使用与北京服务地址匹配的 Key。
`.env` 已在 `.gitignore` 中；`.env.example` 是不含密钥的模板。已有环境变量优先于文件。

在 `/home/bjx/awm-agent` 运行：

```bash
# 不联网、不计费；不显示 Key
agent-world-model/.venv/bin/python -m harness_awm.qwen --check
# 填好 Key 后，发送一次请求，不自动重试
agent-world-model/.venv/bin/python -m harness_awm.qwen '9.9和9.11哪个大？'
```

模型配置在 `configs/harness/qwen3.5-flash.json`。Actor 和 Judge 均使用
`qwen3.5-flash`，请求携带 `enable_thinking=false`，地址为
`https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1`。
当前使用 Chat Completions 接口，与项目现有预算、usage 和请求证据记录共用调用层。
单次测试结果保存在 `runs/qwen_smoke/`，包括实际 usage 和 finish_reason。

上下文预算设为 128000，包含输入、最多 4096 输出和 512 余量。
这是客户端限制，不是传给服务端的 context_length 参数。
沿用项目保守 UTF-8 字节估算，并非 Qwen tokenizer 的精确计数；可能提前拒绝长输入。
价格分档看单次请求实际输入 tokens，账单以服务端 usage 为准。
是否够用需要观察实际任务轨迹；超限会停止，不自动丢弃历史。

Harness 的 `prepare` / `collect` / `evaluate` 可以通过
`--config configs/harness/qwen3.5-flash.json` 选用该配置，CLI 会自动读取根目录 `.env`。
批量运行预算 `max_physical_requests`、`max_total_tokens` 仍为 0，运行前按实际批次设置总上限。
单次测试入口有独立的一次请求预算。历史 rubric/清洗脚本使用各自冻结配置，
部分仍读取 `/home/bjx/app-agent/.env`，不会随此配置切换；新实验需显式接入新配置。

价格请查看 [Qwen3.5-Flash 官方模型页](https://www.qianwenai.com/models/qwen3.5-flash)及账户控制台。

## Rubric 十题试跑（v10，实验版本）

新增 prompt：`configs/rubric/generate_qwen_v10.txt`，固定单题单份 JSON，包含完整示例、五字段、G/P 定义、三值判定及全题 coverage 约束。
试跑结果见 [v10 报告](../runs/rubric_qwen_v10_10_20261024/report_zh.md) 和
[十题原始 rubric](../runs/rubric_qwen_v10_10_20261024/initial/rubrics_zh.md)。
10题共79,989 tokens，按北京普通API公开单价估算¥0.0449346；最大输入8,407 tokens。
结构/引用6/10通过，严格输出契约5/10通过，静态语义审查0/10通过，暂不推广全量。

该试跑显式关闭思考、temperature=0、JSON模式、最多8,000输出tokens；全局Harness思考配置保持原设置。
发送前以UTF-8字节保守估算输入，加输出预留及余量，达到128,000就拒绝发送；不截断原始来源。
每题一次，无自动重试，无付费修复，原始请求/响应和usage存档并与共享账本核对。
此批次已完成；重复run仅复用已存结果，不会再次调用已完成题目。

离线查看与复核（不计API费用）：

```bash
agent-world-model/.venv/bin/python scripts/rubric_qwen_v10.py validate
agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v10.py
agent-world-model/.venv/bin/python -m unittest discover -s tests -p test_rubric_qwen_v10.py -v
```

### 同prompt开启思考的对照试跑

同一v10 prompt和同10题，`enable_thinking=true`、`thinking_budget=16384`，回答上限仍为8000。
模型请求除思考参数外逐项相同；最大实际输入8405 tokens，连同思考/回答预留的保守上界63894，均低于128k。
结果：输入63,893，输出113,912（含思考95,959、回答17,953），共177,805 tokens，估算¥0.2406026。
比非思考版总token增加122.29%，费用为5.35倍；结构/引用仍6/10，严格格式5/10→6/10，静态语义0/10→0/10。
4题触及思考上限，结论限于本轮设置和已知题目，不能推断无限思考或新题表现。

查看 [思考对照报告](../runs/rubric_qwen_v10_thinking_10_20261024/report_zh.md) 和
[思考版10题rubric](../runs/rubric_qwen_v10_thinking_10_20261024/initial/rubrics_zh.md)。
本轮恰好10个唯一请求，均有usage；最多两个请求并发，共用原子预算账本；未增加重试或修复。
思考预留已单独计入预算，服务端usage结算时不会重复计算reasoning_tokens。

离线复核：

```bash
agent-world-model/.venv/bin/python scripts/rubric_qwen_v10_thinking.py validate
agent-world-model/.venv/bin/python scripts/summarize_rubric_qwen_v10_thinking.py
agent-world-model/.venv/bin/python -m unittest discover -s tests -p test_rubric_qwen_thinking.py -v
```
