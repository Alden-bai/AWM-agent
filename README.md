# AWM Harness

P0–P3 的共享执行内核：冻结任务 → 专属 MCP 环境 → 真实轨迹与一致终态 → 官方 SQL+LLM 适配 → 评测／采集／SFT 导出。

- [使用说明](docs/harness_readme_zh.md)
- [验收报告](docs/harness_validation_zh.md)
- [设计基线](docs/harness_design_awm_v1_zh.md)
- [Qwen3.5-Flash 配置与调用](docs/qwen_setup_zh.md)

在本目录运行：

```bash
agent-world-model/.venv/bin/python -m harness_awm --help
```

默认配置使用脚本 Actor、模拟 Judge，付费请求上限为 0；模拟结果不能导出为正式训练数据。真实 API 接口已实现，GRPO 更新和过程 Judge 校准未实现。官方源码及既有清洗产物保持独立。

## 从 GitHub 获取项目

```bash
git clone --recurse-submodules https://github.com/Alden-bai/AWM-agent.git
cd AWM-agent
python -m venv .venv
# Linux / macOS；Windows 使用 .venv\Scripts\Activate.ps1
source .venv/bin/activate
pip install -e ./agent-world-model -e '.[test]'
cp .env.example .env
python -m harness_awm --help
```

正式隔离执行需要 Linux 及 `bwrap`，详见使用说明。官方源码以固定版本的
Git 子模块引用；本地第三方子模块的未提交修改不包含在本仓库中。

数据集需单独下载到清洗脚本约定的目录：

```bash
hf download Snowflake/AgentWorldModel-1K --repo-type dataset \
  --revision dde80a0283fe781bdc51656bce57063dc5650213 \
  --local-dir data/awm-dde80a0
```

`.env`、数据集、`runs/`、`cleaning_runs/`、`evidence/`、日志和数据库仅保留在本地。
文档中引用的历史实验结果不随源码发布，依赖这些产物的示例和测试需要先准备对应数据。
API Key 只填写到本地 `.env` 或环境变量中。发布检查见 [安全说明](SECURITY.md)。
