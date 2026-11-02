# 零 LLM 程序筛查

目标是排除当前环境／判分配置下的已确认缺陷，检查真实工具与官方 SQL Judge 程序能否运行。此流程不解答全部题目，不运行付费模型，也不将程序检查通过标为语义认证通过。

## 运行

使用已安装官方 MCP、FastAPI、jsonschema 等依赖的 Python：

```bash
cd /home/bjx/awm-agent
/home/bjx/app-agent/.audit-venv/bin/python scripts/clean_data.py \
  --scope pilot100 \
  --runtime-checks \
  --probe-cache /home/bjx/awm-agent/cleaning_runs/two_layer_pilot100_v1 \
  --out /home/bjx/awm-agent/cleaning_runs/zero_llm_pilot100_NEW
```

输出目录必须不存在。`pilot100` 使用旧冻结清单的全部 100 个 ID 和原顺序；七类数据资产必须通过原 manifest 的 SHA-256 校验。省略 `--runtime-checks` 时仍执行原结构筛查。

真实 MCP 测试需要本机 loopback socket 权限。沙箱不允许创建 socket 时，需要在允许本机服务的执行环境运行；这与模型联网权限无关。程序不加载 `.env`，不继承 API 凭据与代理，worker 与 MCP 子进程安装只允许 loopback 的 Python socket 审计钩子。Judge 的模型客户端替换为只截获请求、立即终止的客户端，不返回伪造的判分结果。

## 实际检查

1. 原 SQL 初始化、完整性、外键、环境编译、任务与 verifier 唯一绑定。失败按原策略在新数据库复现。
2. 启动原版 MCP 服务并枚举真实工具，检查输入 schema。每次调用先校验参数，再记录真实返回与逐步数据库差异。
3. 从公开响应模型的类型关系定位数据库表，对可映射记录的 ID 和同名标量字段进行独立对照。嵌套对象、列表均支持；时间格式、JSON 编码字段、聚合与筛选业务不因这项检查自动得到认证。读取是否改变数据库单独记录，不无条件认定所有变化都是错误。
4. 为已审查的学徒管理案例保留独立写入／聚合模板，记录具体计算行和目标字段。业务不一致在两份初态数据库复现；只有依赖范围审查的源码哈希匹配时才升级为当前任务缺陷。
5. 用原版 `run_verifier(..., 'sql', ...)` 执行 SQL 取证。初态等于末态是程序运行探针，不预设其为负例；取得真实工具末态后，再执行一次。程序异常用新的数据库副本复现，捕获原始错误。
6. 执行原版 Judge 的提示词构造，在模型传输边界前截获请求，逐项核对题面、真实轨迹和 SQL 结果。没有实际模型判断、模型输出解析或语义可靠性测试。原 SQL 包装器成功取证时返回的 `incomplete` 是该阶段的默认值，不是对本题的模型判分，不能计入任务失败率。

## 旧证据如何复用

- 历史 `requirements.json` 仅作为调用配方候选；不沿用旧模型标签和旧模型生成的 SQL 核查条件。
- 参数值必须能匹配题面字面量，或匹配可核验的公开契约引用；对象 ID 的动态解析只使用前序实际工具结果。禁止 `$sql` 等私有数据库参数来源。
- 在不支持的参数处截断为已支持前缀，记录 `prefix_only`。前缀执行成功不表示整道任务完成。
- 这种字面匹配只建立参数来源，不证明模型选择了正确的业务含义或操作路径。对象不唯一、参数类型错误、未支持的表达式等均属于探针覆盖问题，不能直接判题目有缺陷。
- 已确认的历史 Judge 语义缺陷，在数据、题面和 Judge 源码一致时继续阻止原配置使用；单列历史来源和旧模型配置，不冒充本次发现。模型配置更新后需重新检查。

当前通用模板依赖已有调用缓存；新题没有缓存时仍可检查初始化、工具发现和 SQL 程序，但不能声称完成任务相关工具检查。全库扩展前需要补充共享探针，不能直接将本次覆盖率推广到 10,000 题。

## 输出口径

| 状态 | 含义 |
|---|---|
| `excluded` | 当前环境／Judge 配置不可用；原因区分初始化、工具业务、程序异常、绑定不可用、历史 Judge 语义缺陷 |
| `screen_pass` | 已执行的有限工具／程序检查通过；仍未认证整个任务与 Judge 语义 |
| `not_covered` | 没有任务探针，或执行失败需要归因；不把它们记为坏题 |

所有记录仍为 `training_ready=false`。若需要已验证的训练库，还需要独立的语义准入检查。

`tasks.ledger.jsonl` 是最终逐题台账；`tasks.excluded.jsonl`、`tasks.screen_pass.jsonl`、`tasks.not_covered.jsonl` 为分组。`tasks.reject/retain.jsonl` 是兼容导出；`retain` 包括未覆盖题，不是可直接使用的训练集。`structural/` 保留扩展前结构筛查输出。

每题的 `runtime/<ledger_id>/` 保存配方和来源、进程退出码、工具 schema、真实响应、初末数据库、逐步对照、SQL 结果与截获的 Judge 输入。`runtime_manifest.json` 保存数据之外的脚本／官方源码哈希及解释器路径；运行时间使用真实时钟。

## 验证

```bash
/home/bjx/app-agent/.audit-venv/bin/python -m pytest \
  tests/test_clean_data.py tests/test_zero_llm.py tests/test_two_layer.py -q
```

回归覆盖：禁止私有 ID／未来结果引用、公开契约引用核查、网络及凭据隔离、重复行差异、嵌套返回字段核对、占位写入误成功、探针失败与任务拒绝分离、原 SQL 异常及 Judge 请求截获。
