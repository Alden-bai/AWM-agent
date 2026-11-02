# 候选代码检查与100题prompt交付

本次只检查代码和准备prompt，没有调用模型，也没有审查、改写之前Luna生成的rubric。旧代码与历史结果未修改；下面的边界缺陷尚未修复，新试跑调度prompt要求在独立版本修复并测试后运行。

## 已验证

- `scripts/rubric_candidates.py` 的10个现有测试通过。
- 离线遍历 `data/awm-dde80a0/gen_tasks.jsonl`：1000个环境、10000题，共45590个候选；未发现非空题面字符漏覆盖或任务ID重复，最长题面800字符。记录在 `runs/rubric_candidate_code_audit_20261024/dataset_coverage.json`。
- 这只证明切片保留字符，不证明候选是正确业务义务，也不证明接口来源预处理或最终rubric无误。完整题面必须继续交给生成器和审查器。

## 可复现的问题

1. `prepare_input.walk` 用 `json.dumps(v, ensure_ascii=False)` 在原JSON中查找字符串，遇到原来源把中文保存为 `\uXXXX` 时会报 `nonoriginal_reference`。应按真实JSON字符串token及其路径定位原文offset，不重写原来源。
2. `walk` 在所有层级无条件删除 `example/examples` 键，导致 `request_params.example` 这样的真实参数定义也从模型视图中消失。还原校验能成功，但不能证明模型视图没有丢失业务字段。应区分字段名映射与示例元数据；无法确定时保留。
3. `s['text'].index(q)` 总取第一次出现的位置，两个路径的描述相同时会绑定同一个offset。引文仍在来源中，但记录的pointer和实际字符位置不一致。需要按JSON路径绑定对应出现位置；不能用重复字符串去重代替位置定位。
4. 分段只保护ASCII引号；`“A, B and C”`会被拆成多个候选，ASCII单引号中的英文撇号也可能提前结束保护。这通常不丢字符，但违反引号整体保护的预期并增加模型恢复上下文负担。智能引号、内部撇号和括号应有回归样例；不要把词法片段当独立义务。

复现入口：`agent-world-model/.venv/bin/python scripts/audit_rubric_candidates.py`。它只执行离线反例，记录到 `runs/rubric_candidate_code_audit_20261024/edge_cases.json`，不会调用模型。

## 流程接入问题

- `K0001` 是契约片段ref_id；`C01` 是候选ID；`contract:xxx/schema:xxx/task`等实际source_id是来源文档ID。上一轮的失败输出把K引用填进evidence.source_id。校验器拒绝它是正确的，生成prompt和错误反馈应明确这三个命名空间，不能默默转换后把旧结果算通过。
- 旧 `evaluate_scores` 把含1分的合法评分标为review；当前验收必须沿用 `rubric_qwen_v13.evaluate` 的“合法五维无0即通过”，不能直接恢复旧门槛。
- `rubric_luna56_lean_run.py`固定为10题，原输入prepare使用手工选择的接口集合。扩大到100/10000题需要动态manifest和统计分母、通用的公开来源准备，不能只改数量。

## Prompt文件

- `configs/rubric/luna_high_100/run_100.txt`：给主调度agent的完整100题启动prompt；只允许代码检查、调度、统计，不允许主agent审查rubric语义。
- `generate.txt`：原select_compile_v13业务prompt全文，加ID命名空间澄清。
- `judge.txt`：原judge_qwen_v13业务prompt全文，评分标准未改。
- `revise.txt`：生成业务prompt及修订方式，仍返回完整版本。
- `execute.txt`：单题单阶段、只读包与写结果、简洁输出、真实新上下文、不设人为单次上限。

每个模型输入包同时使用execute与对应阶段的protocol；生成、审查和修订共享相同来源与当前schema。不要把主调度prompt或整个题库交给每个子agent。运行时实际配置model和high，prompt中的名称不具备切换模型的作用。

本次只交付提示词及代码检查，100题runner和边界修复需由执行这份启动prompt的后续任务完成；未启动100题试跑，也未验证新ID补充规则后的通过率或token变化。
