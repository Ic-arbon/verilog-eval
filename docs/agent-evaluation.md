# Agent 测试说明

本文说明如何使用 VerilogEval 在同一个本地 vLLM 模型上测试 Pi 和 OpenCode，并读取、比较和排查测试结果。

## 1. 测试目标

Agent 测试与 model-only 测试使用相同的 Verilog 题目和隐藏评分器，但允许 Agent 在提交前读取任务、编写代码、创建自测文件并调用 `iverilog`。

建议比较以下三组结果：

- model-only：模型直接生成一次答案；
- Pi：模型通过 Pi 的工具循环完成任务；
- OpenCode：模型通过 OpenCode 的工具循环完成任务。

公平比较时，应固定仓库提交、模型、vLLM 地址、题目集合、超时和评分器版本。

## 2. 执行架构

每道题使用一个独立工作区。Agent 只能看到：

```text
TASK.md
AGENT_INSTRUCTIONS.md
TopModule.sv             # Agent 创建
Agent 自己创建的测试文件
Agent 配置和缓存目录
```

Agent 看不到数据集仓库中的 `*_ref.sv` 和 `*_test.sv`。Agent 退出后，宿主监督器才会使用隐藏文件编译和仿真 `TopModule.sv`。

默认隔离模式为 `--sandbox auto`：

1. 如果无特权 user namespace 可用，使用 Bubblewrap；
2. Bubblewrap 不可用时，自动使用 Docker；
3. 两者都不可用时，在启动任何题目之前直接报错。

Docker 容器使用只读根文件系统、非 root UID、空 capabilities，并且不挂载 Docker socket。只有当前题目的 `/workspace` 可写。

## 3. 前置条件

运行环境需要：

- `x86_64-linux`；
- Nix Flakes；
- 健康的本地 vLLM，默认地址为 `http://127.0.0.1:58000/v1`；
- Bubblewrap 或当前用户可访问的 Docker daemon；
- 足够的 `/opt` 磁盘空间用于 Nix、Docker、轨迹和缓存。

检查服务：

```bash
curl --fail http://127.0.0.1:58000/health
docker info >/dev/null
```

如果 Docker 数据也要放在 `/opt`，可在 `/etc/docker/daemon.json` 中配置：

```json
{
  "data-root": "/opt/docker"
}
```

修改后验证并重启：

```bash
sudo dockerd --validate --config-file=/etc/docker/daemon.json
sudo systemctl restart docker
docker info | grep 'Docker Root Dir'
```

## 4. 推荐入口与缓存

推荐使用包装脚本：

```bash
./scripts/agent-eval [测试参数]
```

包装脚本内部仍然执行 `nix run .#agent-eval`，但会在 Nix 启动前自动创建并设置：

```text
<仓库>/.cache
```

在 `/opt/agent/verilog-eval` 部署时，默认缓存位置就是：

```text
/opt/agent/verilog-eval/.cache
```

需要指定其他位置时：

```bash
VERILOG_EVAL_CACHE_ROOT=/opt/verilog-eval-cache \
  ./scripts/agent-eval --agent opencode --problems Prob001_zero
```

直接使用 Nix 也可以，但若要避免 Nix 使用家目录缓存，必须在 `nix run` 之前设置环境变量：

```bash
XDG_CACHE_HOME="$PWD/.cache" \
VERILOG_EVAL_CACHE_ROOT="$PWD/.cache" \
nix run .#agent-eval -- \
  --agent opencode \
  --problems Prob001_zero
```

## 5. 单题烟雾测试

不要直接从 156 题开始。先分别验证一个 Agent、一题和一个并发任务。

### OpenCode

```bash
cd /opt/agent/verilog-eval
git pull

./scripts/agent-eval \
  --agent opencode \
  --problems Prob001_zero \
  --jobs 1
```

### Pi

```bash
./scripts/agent-eval \
  --agent pi \
  --problems Prob001_zero \
  --jobs 1
```

正常启动时应看到：

```text
External agents ready: pi=0.82.1 opencode=1.18.7
Sandbox backend: docker
Running 1 trajectories with 1 parallel jobs
```

最终结果应明确显示 `PASS` 或其他 Agent/评分状态。例如：

```text
[opencode] Prob001_zero: PASS
opencode: 1/1 passed
```

## 6. 分阶段扩大测试

单题通过后，建议按以下顺序增加负载。

### 小批量

```bash
./scripts/agent-eval \
  --agent opencode \
  --problems Prob001_zero Prob002_m2014_q4i Prob003_step_one \
  --jobs 2
```

### 单 Agent 全量

```bash
./scripts/agent-eval \
  --agent opencode \
  --jobs 16 \
  --timeout 180
```

### 两个 Agent 全量

```bash
./scripts/agent-eval \
  --agent all \
  --jobs 16 \
  --timeout 180
```

`--jobs` 是所有轨迹共享的总并发数，不是每个 Agent 各自的并发数。若 vLLM 出现排队、超时或显存压力，应先降到 `4` 或 `8`。

## 7. 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--agent` | `all` | `pi`、`opencode` 或 `all` |
| `--task` | `spec-to-rtl` | `spec-to-rtl` 或 `code-complete-iccad2023` |
| `--model` | `qwen3.6-coder` | 发送给两个 Agent 的模型 ID |
| `--base-url` | `http://127.0.0.1:58000/v1` | OpenAI-compatible vLLM 地址 |
| `--problems` | 全部题目 | 空格或逗号分隔的问题 ID |
| `--jobs` | 最多 16 | 并行轨迹数量 |
| `--timeout` | `180` | 每个 Agent 轨迹的秒数上限 |
| `--sandbox` | `auto` | `auto`、`bwrap` 或 `docker` |
| `--run-root` | 自动生成 | 指定结果根目录 |
| `--dry-run` | 关闭 | 只生成工作区和沙箱命令，不调用 Agent |

测试 code-complete 数据集：

```bash
./scripts/agent-eval \
  --agent opencode \
  --task code-complete-iccad2023 \
  --problems Prob001_zero \
  --jobs 1
```

## 8. 结果目录

每次运行会创建：

```text
runs/agent-eval-<UTC时间>/
├── summary.csv
├── summary.json
└── <agent>/
    └── <problem>/
        ├── command.json
        ├── trajectory.jsonl
        ├── stderr.log
        ├── grade.log
        ├── metrics.json
        ├── simulation
        └── workspace/
            ├── TASK.md
            ├── AGENT_INSTRUCTIONS.md
            ├── TopModule.sv
            └── Agent 创建的其他文件
```

主要文件：

- `summary.csv`：适合表格分析和 Agent 间比较；
- `summary.json`：整次运行的结构化结果；
- `metrics.json`：单题 Agent 状态、评分状态、耗时、token 和工具调用；
- `trajectory.jsonl`：Agent 的原始 JSONL 事件；
- `stderr.log`：Agent 或沙箱启动错误；
- `grade.log`：隐藏评分器的编译和仿真输出；
- `command.json`：实际执行的沙箱命令，用于复现启动问题；
- `workspace/TopModule.sv`：Agent 最终提交。

## 9. 状态解释

Agent 状态与评分状态是两个不同层次。

### Agent 状态

| 状态 | 含义 |
| --- | --- |
| `completed` | Agent 正常退出并提交了 `TopModule.sv` |
| `agent_error` | Agent CLI 或沙箱命令非零退出 |
| `timeout` | Agent 超过 `--timeout` |
| `missing_submission` | Agent 正常结束但没有生成 `TopModule.sv` |
| `dry_run` | 只生成命令，没有执行 Agent |

### 评分状态

| 状态 | 含义 |
| --- | --- |
| `passed` | 隐藏测试输出零 mismatch |
| `failed` | 仿真完成但存在 mismatch |
| `compile_error` | 候选代码与隐藏测试无法编译 |
| `timeout` | 隐藏仿真超时 |
| `missing_submission` | 没有可评分的 `TopModule.sv` |

`completed` 不等于 `passed`。它只表示 Agent 执行正常；最终正确性必须查看 `grade.status` 和 `passed`。

测试命令的退出码主要表示监督器是否完成所有轨迹，不表示所有题目都通过。通过率应读取 `summary.csv` 或终端中的 `x/y passed`。

## 10. 指标说明

每题记录：

- `duration_seconds`：Agent 运行时间；
- `turns`：Agent/model 交互轮数；
- `tool_calls`：工具调用次数；
- `input_tokens`：各模型步骤输入 token 总量；
- `output_tokens`：各模型步骤输出 token 总量；
- `parse_errors`：无法解析的 JSONL 行数；
- `passed`：隐藏测试是否通过。

比较 Agent 时，至少报告：

```text
通过题数 / 总题数
通过率
平均耗时
总输入/输出 token
平均 turns
平均 tool calls
Agent 版本
仓库提交
模型与 endpoint
沙箱后端
```

## 11. 常见故障

### Bubblewrap UID map 被拒绝

错误：

```text
bwrap: setting up uid map: Permission denied
```

使用默认 `--sandbox auto` 会自动回退 Docker。也可明确指定：

```bash
./scripts/agent-eval --sandbox docker --agent opencode --problems Prob001_zero
```

### Docker 不可访问

检查：

```bash
docker info
```

若出现权限错误，将当前用户加入 Docker 组并重新登录：

```bash
sudo usermod -aG docker "$USER"
```

### 所有任务立即 `agent_error`

先停止全量测试，查看第一题：

```bash
run=$(ls -dt runs/agent-eval-* | head -1)
cat "$run/opencode/Prob001_zero/stderr.log"
cat "$run/opencode/Prob001_zero/command.json"
```

### Agent `completed` 但未通过

依次检查：

```bash
run=$(ls -dt runs/agent-eval-* | head -1)
cat "$run/opencode/Prob001_zero/workspace/TopModule.sv"
cat "$run/opencode/Prob001_zero/grade.log"
cat "$run/opencode/Prob001_zero/metrics.json"
```

这通常表示代码逻辑错误、端口错误、编译错误或隐藏测试 mismatch，而不是 Agent 启动失败。

### `iverilog` 出现 stack smashing

确保使用最新仓库版本。评分器会清除宿主的 `LD_LIBRARY_PATH` 和 `LD_PRELOAD`，避免 Docker glibc 污染隐藏评分流程：

```bash
git pull
```

### 大批量超时

先降低并发并增加单题超时：

```bash
./scripts/agent-eval \
  --agent opencode \
  --jobs 4 \
  --timeout 300
```

## 12. 公平比较检查表

开始正式对比前确认：

- 三组测试使用相同 Git commit 和数据集；
- Pi 与 OpenCode 使用相同模型 ID 和 vLLM endpoint；
- 使用相同题目列表和隐藏评分器；
- 使用相同超时与最大输出 token；
- 不在测试中途修改 prompt 或 Agent 版本；
- 保存原始 JSONL、stderr、grade log 和 summary；
- 记录 Pi、OpenCode、Docker、Nix 和模型版本；
- 先完成单题与小批量验证，再运行完整数据集。

版本记录示例：

```bash
git rev-parse HEAD
cat .agent-tools/.versions
docker version --format '{{.Server.Version}}'
nix --version
curl -s http://127.0.0.1:58000/v1/models
```

完成上述检查后，再使用固定命令分别运行 Pi、OpenCode 和 model-only 基线。
