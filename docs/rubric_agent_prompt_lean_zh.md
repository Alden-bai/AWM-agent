# 子 agent 单题执行 prompt

上一次 Luna 试跑的四个子 agent 实际读取了 `select_compile_v13.txt` 或 `judge_qwen_v13.txt`，所以确实加载了来源绑定、五字段、硬软分类、五维评分等业务约束。加载不等于每条语义约束都执行正确。四个角色共进行了 70 次模型响应，累计 4,087,261 tokens，其中缓存输入 3,704,832、非缓存输入 288,621、输出 93,808；缓存输入属于总输入，不能重复相加，也不能按普通输入价格直接换算费用。

上次同一个 agent 连续处理多个题目，工具读取也较宽：四个角色分别有 18、15、19、14 次工具调用，工具输出合计约 55 万字符。还发生了错误 skill 路径读取后的补读。业务 prompt 中的“只输出 JSON”约束了结果格式，却没有充分限制工具循环和历史上下文增长。

新的 `configs/rubric/agent_execution_lean_v1.txt` 是执行层约束，原生成和审查业务 prompt 原样嵌入，不删业务规则：

- 一个上下文只处理一道题、一个阶段；调度时使用 `fork_turns=none` 和指定的 `gpt-5.6-luna/high`。审查单独新建上下文。
- 一次读取完整输入包，一次写出结果是执行目标。只让模型做筛选、分类、业务谓词或评分，代码负责映射校验、原文引用装配、汇总统计。
- selection 理由和评分发现保持简洁，保留必要反例；不重复输出整份 rubric，不浏览历史运行或其他题目。
- 修订最多一轮，仅向修订生成器提供反馈；审查输入采用明确字段白名单，拒绝携带历史反馈。通过标准仍是合法五维评分没有 0 分。

离线入口 `scripts/prepare_rubric_agent_prompts.py` 可用 `--manifest` 为十道题准备初次生成包，或用 `--input` 和 `--role generate|judge|revise` 为单题准备指定阶段。输入数据只压缩 JSON 空白，逐值保持一致；包含的原 prompt 不变。输出目录必须新建，结果文件不得覆盖。

```bash
agent-world-model/.venv/bin/python scripts/prepare_rubric_agent_prompts.py \
  --manifest runs/rubric_luna56_agents_10_20261024/manifest.json \
  --destination runs/rubric_luna56_prompt_lean_prepared_20261024
```

该命令已离线执行，十个包状态为 `prepared_not_run`。每个目录的 `launch.txt` 给未来调度使用；脚本本身不启动 agent、修订循环或模型调用。后续审查须打包新生成的当前 rubric，不能沿用历史评分。已检查十题资料和原 prompt 完全保留、审查拒绝历史字段、已有包拒绝覆盖。

原输入已经接近紧凑 JSON，十题数据字符数仅从 171,472 变为 171,462，因此这次节省的重点是减少工具循环和重复传入历史，不是删除空白。

工具次数和 128k 在 prompt 中都是执行要求，准备脚本没有实现模型运行时硬上限。拆成单题上下文可避免跨题历史累积，但会增加每个上下文固定开销；总 token、缓存比例及通过率的净变化尚未试跑，不能给出已节省的百分比。字符数量变化不是 token 用量，更不是费用。原来的 60% 首次、90% 最终通过率只属于上一次试跑。
