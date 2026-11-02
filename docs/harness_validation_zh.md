# AWM Harness P0–P3 验收报告

日期：2026-10-23。交付范围：运行内核、采集／评测、补评、诊断及 SFT 导出。真实执行环境；Actor 和 Judge 使用离线替身；付费 API 请求为 **0**。

## 1. 实现结果

`python -m harness_awm` 已提供 `prepare / collect / evaluate / reevaluate / diagnose / export-sft` 六个入口。共享内核实现严格单 JSON 动作、独立 MCP 世界、逐轮真实输入输出、观察外置／回读、状态证据、进程静止确认、SQLite 一致快照、官方 SQL verifier 和原版 Judge 请求／解析适配。

脚本 Actor 与真实 API backend 分开，模型和 Judge 分别限流；默认全局付费请求上限为 0。有效判分、奖励准入、执行完整性、终态可信度、清洗状态分别记录。模拟判分不能进入正式奖励或 SFT。

正式配置需要显式 split、来源族／世界隔离、工具哈希、Judge 配置准入和时间检查。过程评分与 GRPO 接口会明确报未实现。

## 2. 自动化测试

**30 个新增测试及 11 个既有清洗测试通过。** 完整新增套件执行了 29 项；随后新增 API 错误留痕测试并定向运行全部 5 项阶段测试，另定向复测了加强后的答复结算键用例。去重后是 30 个新增测试，不将重复回归次数计为新测试数。

| 验证 | 结果 | 原始日志 |
|---|---|---|
| Harness 完整套件 | 29/29，通过，83.379 秒 | `evidence/harness_validation/harness_full_29.log` |
| 最终阶段／API／导出回归，含新增错误用例 | 5/5，通过；其中 4 项与前行重叠 | `evidence/harness_validation/stages_final_5.log` |
| 最终动作／存储／奖励／配置哈希回归 | 12/12，通过，与完整套件重叠 | `evidence/harness_validation/unit_final_12.log` |
| 加强后的答复结算键检查 | 同 rollout、同初末 DB、同配置，仅改答复，键不同 | `evidence/harness_validation/answer_key_final_1.log` |
| 原有清洗回归 | 11/11，通过 | `evidence/harness_validation/cleaning_regression_11.log` |

覆盖：多 JSON／重复键／非有限数／非法参数不执行；未知工具拒绝；观察越界及跨轨迹读取拒绝；WAL 一致备份；部分初始化拒绝；并发预算原子预留；未知用量不释放；工具和私有评测文件隔离；四候选不串库；写超时／服务退出／用户取消；上下文和轮次耗尽；Judge 截断、SQL 异常；最终答复实际进入官方请求；补评不重跑 Actor；测试及篡改轨迹禁止导出；事件缺口禁止导出；SFT mask 只覆盖当前 assistant 响应。

API 单测替换了传输客户端，验证参数别名、无自动重试、预算、错误留痕及凭证脱敏。没有对外部 API 进行连通性或模型效果验收。

## 3. 四道真实 AWM 开发任务

冻结任务库：`runs/offline_registry_v4`。最终采集运行：`runs/keep4_execution_v2`。独立核查结果：`evidence/harness_validation/keep4_checks.json`。

| 原任务 | 真实工具调用 | 观察到的写操作 | 独立终态字段检查 |
|---|---:|---:|---|
| `it_service_management_itsm_1:1` | 2 | 1 | 新增符合 category、impact、描述及 logged 状态的 incident |
| `social_networking_8:0` | 2 | 1 | 当前用户新增指定文本且 audience=Friends 的 post |
| `education_training_management_1:8` | 3 | 1 | competency 340 的 level/category/描述满足指定值 |
| `e_commerce_10:7` | 2 | 1 | 目标 SKU 新增指定文本、5 星、pending 状态的 review |
| 合计 | **9** | **4** | **4/4 有限字段检查通过** |

4 条轨迹均明确 finish、保存可信终态，9 次工具响应均无 MCP `isError`；官方 SQL verifier 实际执行并产生取证结果。Judge 为测试替身，所以官方真实判分数和有效奖励结算数均为 0，不将上述 4/4 字段检查称为模型成功率或全面业务正确性。

补评前后，初末 DB、轨迹和事件哈希保持一致，已有有效测试判分直接复用，没有新增判分 attempt。诊断的证据问题数为 0。SFT 正式导出为 0 条，排除原因为 development/test-only，符合预期。

复核命令：

```bash
agent-world-model/.venv/bin/python scripts/validate_harness_run.py \
  --run runs/keep4_execution_v2 \
  --output evidence/harness_validation/keep4_checks.json
```

该脚本保留逐题检查 SQL 和参数，核对冻结证据、幂等补评、诊断及导出排除规则；不会调用模型。

## 4. 动态发现与修复

- uv 虚拟环境解释器经版本别名软链接定位，严格沙箱需同时挂载实际 Python 路径和该别名。
- 当前宿主的受限环境对异步子进程通知存在兼容性问题，退出确认改为 `Popen.poll`，并对进程组进行有界终止／强杀。
- 工具超时可能触发 MCP/AnyIO 取消作用域，已将会话生命周期放到专属任务中；Actor 超时不会破坏收尾。相关超时和取消复测通过。
- Pydantic 默认浮点字段在保存再加载后可能从整数表示变为浮点表示，影响判分配置哈希。合同启用了默认值验证，并增加 JSON 往返哈希一致测试；最终真实运行的补评复用验证通过。
- 电商环境的样本 SQL 使用当前时间，重新初始化会改变多个日期字段。保留清洗时的冻结世界，记录重建不一致，拒绝自动赋予该差异配置正式奖励准入；没有将新时间世界冒充原实例。

## 5. 验证边界

- 严格模式验证了文件系统白名单和环境／评测进程分离；使用共享宿主回环网络，不提供网络命名空间隔离，不声称防御恶意服务扫描其他端点。
- 真实远端 Actor/Judge 尚未运行；官方判分语义可靠性、过程规则校准、GRPO 更新及训练收益尚未验证。
- token mask 的单测验证了模板前缀及监督位置合同；未绑定具体训练模型 tokenizer。真实 token 导出需要使用说明中指定的本地 tokenizer 和 chat template，并通过相应模型的模板检查。
- 跨进程崩溃仅识别不确定写入和证据缺口，不承诺任意现场恢复或 exactly-once。工作进程未知状态不能靠补跑业务动作消除。
- `runs/` 下保留了开发失败及中间试跑。正式验收以本报告列出的 `keep4_execution_v2`、测试日志和实现校验和为准；旧试跑不并入最终指标。

最终实现文件校验和见 `evidence/harness_validation/implementation_checksums.json`。官方 AWM 源码和既有清洗产物未修改。
