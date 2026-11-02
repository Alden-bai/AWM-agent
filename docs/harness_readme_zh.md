# AWM Harness 使用说明（P0–P3）

代码入口是 `harness_awm`，设计依据是同目录的 `harness_design_awm_v1_zh.md`。实现支持冻结任务导入、共享 EpisodeRunner、真实 MCP、采集／评测、官方 SQL+LLM 适配、终态补评、只读诊断和 SFT 导出。GRPO Actor、训练更新和过程评分接口会显式报未实现。

## 1. 环境与第一次运行

当前工作目录为 `/home/bjx/awm-agent`。已有的 `agent-world-model/.venv/bin/python` 提供官方 AWM 运行依赖，也可直接运行 Harness。顶层 `pyproject.toml` 独立声明 Harness 依赖；新环境需同时安装官方项目依赖。无需修改官方源码。

```bash
cd /home/bjx/awm-agent
agent-world-model/.venv/bin/python -m harness_awm --help

# 每次使用新的输出目录；已有运行不会被覆盖。
agent-world-model/.venv/bin/python -m harness_awm prepare \
  --source cleaning_runs/two_layer_pilot100_v1 \
  --output runs/my_registry \
  --config configs/harness/offline.json

agent-world-model/.venv/bin/python -m harness_awm collect \
  --registry runs/my_registry \
  --run runs/my_collect \
  --config configs/harness/offline.json \
  --scripts configs/harness/keep4_scripts.json

agent-world-model/.venv/bin/python -m harness_awm evaluate \
  --registry runs/my_registry \
  --run runs/my_evaluation \
  --config configs/harness/offline.json \
  --scripts configs/harness/keep4_scripts.json
```

这组命令真实执行四个 AWM 环境的读取／写入，但模型输出来自预写脚本，Judge 是固定返回的测试替身。`test_only=true`、`reward_valid=false`、`reward=null` 是预期行为，不能将它们当作模型成功率或训练数据。`discovery_scripts.json` 则只发现工具，不完成题目。

`prepare` 验证清洗 manifest、keep 清单、keep audit 的校验和，核对原始资产文件、任务文字和 verifier 代码绑定；重新执行官方初始化检查完整性，随后复制**清洗时冻结的初态**。不以新的时间相关初始化替换旧世界。如果重建世界与冻结世界不同，记录在 registry 的 `initialization_validation.json`，并阻止自动正式奖励准入。

## 2. 配置、隔离与真实 API

配置字段由 `RunConfig` 严格验证，未知字段会报错。默认每题 32 轮、32 次业务调用、600 秒，每次工具等待 60 秒；输入窗口 32768、预留输出 2048、余量 512，累计 token 上限 131072。API 输入计数为 UTF-8 序列化字节保守估算，不冒充服务 tokenizer 的精确计数。`seed` 记录调度来源，当前固定顺序不做随机采样；它不承诺远端模型确定性。

- `workers` 控制环境并发；`model_concurrency`、`judge_concurrency` 分别限制模型和判分并发；`candidates_per_task` 冻结每题候选数。候选全部预先写入 manifest。
- 默认 `isolation=strict`：需要 Linux `bwrap`、用户／PID 命名空间和本机回环 socket 权限。只挂载 Python 依赖、生成代码及单个环境工作目录；SQL worker 只获得评测副本。隔离不可用时记录失败，不自动降级。
- `isolation=development` 是显式的无文件沙箱调试模式，不能用于正式奖励。严格模式提供文件系统隔离，网络使用宿主回环地址，**不是网络沙箱，也不声称防御恶意环境主动扫描其他端点**。
- 使用专属进程组及 PID 命名空间收尾。MCP 会话由独立任务管理，调用超时／取消不破坏收尾逻辑。可能写入的调用不重试；确认进程退出后才备份终态。不能确认静止则不结算有效奖励。
- 外部 LLM 凭证不会传给环境或 verifier。默认从不自动读取其他项目的 `.env`。API 凭证用配置中的 `api_key_env` 指定，值不写入 manifest 和请求日志。

真实 API 模板是 `configs/harness/api.example.json`。分别填写 Actor/Judge endpoint、model、密钥环境变量名，并在获授权时明确设置本次 `max_physical_requests` 和 `max_total_tokens`。默认两者均为 0。SDK 自动重试固定关闭；模型/Judge 每次物理请求统一占用预算，失败或取消后未知用量继续占据预留。此实现不会继承清洗任务的旧调用预算。

官方 Judge 的提示词、温度和输出上限来自当前官方实现；`judge_api.temperature` 不改写官方请求。`max_tokens_alias=true` 仅将 `max_completion_tokens` 映射为 `max_tokens`，同时保存原始官方请求和实际传输请求。原版解析器保留，Harness 另行拒绝非严格 JSON、截断和非法分类。

正式运行还需要：`purpose=formal`、真实 Actor/Judge、严格隔离、冻结 split、工具目录哈希、对应 Judge 配置准入和时间语义检查。`prepare --splits` 接受 `{ "scenario": "train|dev|test" }`；同来源族或同逻辑初态不得跨 split。`--admissions` 接受按 `task_instance_id` 索引的对象，每项包含：

```json
{
  "reward_ready": true,
  "tool_schema_hash": "已验收工具目录哈希",
  "judge_config_hash": "已验收判分配置哈希",
  "relative_time_resolved": true,
  "evidence": "独立准入证据引用"
}
```

这些是外部验收声明，不是通过填 `true` 完成校准；Harness 不生成可靠性证书。工具哈希来自运行的 `tool_catalog.json`；Judge 哈希由 `harness_awm.evaluation.judge_config_hash(config)` 计算，绑定官方 verifier 源码、API 配置及奖励协议。默认准备的 registry 全是 development，reward readiness 为 false。

## 3. 补评、诊断、导出

```bash
agent-world-model/.venv/bin/python -m harness_awm reevaluate --run runs/my_collect
agent-world-model/.venv/bin/python -m harness_awm diagnose --run runs/my_collect
agent-world-model/.venv/bin/python -m harness_awm export-sft \
  --run runs/my_collect --output runs/my_collect/exports/offline_check
```

- `reevaluate` 验证冻结证据和官方代码版本。已有首个有效分类直接复用；未完成评测可以补评同一终态，不调用 Actor、不改变动作或最终答复。预算仍使用原运行累计账目，不支持改配置扩大旧运行预算。
- `diagnose` 只读检查哈希、事件顺序和未完成写入；不会修改原运行，也不会自动重放写操作。继续实验应创建新 run，保留旧记录。
- `export-sft` 只接受 formal/train、真实有效成功、显式 finish、可信终态、完整事件链及未篡改的逐轮材料。离线示例导出 0 条是正确结果。
- 默认导出每轮原始 `prompt_messages` 和 `response`，损失目标只有当前 assistant 动作；历史、用户与环境输入为条件。过程质量为 `not_evaluated`。
- 可同时传 `--tokenizer <本地目录>`、`--chat-template <本地模板文件>`，通过可选 `tokens` 依赖导出 `input_ids/labels/response_mask`。只加载本地文件、不运行远端 tokenizer 代码；模板必须保持实际输入的 generation prompt 前缀。提示 token 的 label 为 -100。这是 SFT 的本地编码，不能声称是黑盒 Teacher 的真实采样 token 或 GRPO on-policy 数据。

`collect` 和 `evaluate` 共用执行内核；前者记录采集用途，后者保留固定候选评测分母。正式 collect 仅允许 train split。任何 init 失败、预算停止或无效评测均留在计划候选数中。

## 4. 证据目录与指标

```text
runs/<run>/
  manifest.json, manifest.sha256.json, budget.sqlite, metrics.json
  episodes/<rollout>/a0/
    task.json, initial.db, final.db, tool_catalog.json
    runtime/working.db, runtime/server.py
    events.jsonl, model_turns/, blobs/, audit/
    trajectory.json, evidence_seal.json, episode_result.json
    evaluations/<input_hash>/attempt_0000/
      sql/, request_build/, official_request.json, raw_response.json, result.json
  failures/, exports/
```

`runtime/working.db` 是唯一业务写入库；初末快照不直接交给 Actor。raw MCP、已展示观察和私有审计分层保存；外置观察只能用当前 episode 的 ID 读取。`diagnose` 对事件缺口报 `outcome_unknown`，不保证跨进程 exactly-once 恢复。

`metrics.json` 同时报计划题数／候选数、奖励有效结算数、成功分子与分母、真实官方判分数、test-only 数、各类终止和无效原因、观察截断／回读、时间、token 估算和物理 API 请求。无效不等于已证实业务失败。四候选不能当四个独立任务样本。

## 5. 测试

```bash
# 需要允许本机回环 TCP；所有模型调用均为测试替身。
PYTHONPATH=tests:. agent-world-model/.venv/bin/python -m unittest discover \
  -s tests -p 'test_harness_*.py' -v

# 既有清洗回归，使用原有 pytest 环境。
python -m pytest tests/test_clean_data.py tests/test_two_layer.py -q
```

集成测试使用明确标记的合成 FastAPI/MCP 环境，覆盖真实读写、长返回、四候选隔离、文件越界、超时、服务退出、用户取消、只读答复、官方 SQL 取证和 Judge 请求构造。当前真实 AWM 开发试点及验收边界见 `harness_validation_zh.md`。
