# Agent 评测指南

## 1. 唯一评测路径

Agent 只替换单个 sample 的生成程序，不替换调度器或 grader：

```text
configure
  → GNU Make
  → scripts/sv-agent-generate
  → 每题独立 Docker workspace
  → /workspace/TopModule.sv
  → canonical *_sampleNN.sv
  → 原始 Icarus hidden tests
  → scripts/sv-iv-analyze
  → summary.csv / summary.txt
  → scripts/sv-agent-analyze
  → agent-summary.json / agent-summary.txt
```

关键不变量：

- `configure` 加 GNU Make 是唯一问题/sample 调度器。
- Model 与 Agent 共享进程/文件层面的 Generator Program Protocol，不共享
  Python backend 基类。
- `/workspace/TopModule.sv` 是唯一正式 Agent 提交。
- 聊天文本、Markdown代码块和stdout永远不会被提取为candidate。
- Icarus、隐藏测试和`sv-iv-analyze`是唯一正确性权威。
- Agent运行状态、提交状态和Verilog正确性分别记录。
- 正式Pass@1不向Agent反馈隐藏grader结果，也不因grader失败重试。

详细设计见
[`agent-generator-protocol-v1.md`](agent-generator-protocol-v1.md)。

## 2. 单题起步

要求：

- x86_64 Linux与Nix；
- 可用的Docker daemon；
- `http://127.0.0.1:58000/v1`上的`qwen3.6-coder`；
- `/opt/llm/api-key.env`中的可选`VLLM_API_KEY`，或显式
  `OPENAI_API_KEY`；
- 显式`AGENT_EVAL_AGENT_TOOLS`前缀，其中包含
  `node_modules/.bin/<selected-agent>`。

OpenCode单题：

```bash
cd /opt/agent/verilog-eval
printf 'Prob001_zero\n' >/tmp/agent-smoke.txt
AGENT_EVAL_AGENT_TOOLS="$PWD/.agent-tools" VERILOG_EVAL_JOBS=1 nix run .#agent-eval -- --with-agent=opencode --with-problems=/tmp/agent-smoke.txt
```

Pi单题：

```bash
AGENT_EVAL_AGENT_TOOLS="$PWD/.agent-tools" VERILOG_EVAL_JOBS=1 nix run .#agent-eval -- --with-agent=pi --with-problems=/tmp/agent-smoke.txt
```

命令建议保持单行；若拆行，每一行结尾必须保留反斜杠。否则后续
`--with-*`会被shell当作新命令。

Nix app会：

1. 要求并验证显式Agent tools前缀；
2. 计算完整tools内容摘要，不依赖目录名或mtime；
3. 加载固定的Docker镜像archive；
4. 检查vLLM health endpoint；
5. 创建包含源码、endpoint、镜像与tools摘要的参数哈希目录；
6. 调用`configure`并`exec make`。

需要强制创建全新实验根目录时：

```bash
AGENT_EVAL_AGENT_TOOLS="$PWD/.agent-tools" \
VERILOG_EVAL_BUILD_ROOT="$PWD/build/my-fresh-run" \
VERILOG_EVAL_JOBS=1 \
nix run .#agent-eval -- --with-agent=opencode --with-problems=/tmp/agent-smoke.txt
```

## 3. 配置参数

Nix Agent app默认值：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--with-generator` | `agent` | 选择Agent producer |
| `--with-agent` | `opencode` | `opencode`或`pi` |
| `--with-model` | `qwen3.6-coder` | 真实endpoint模型ID |
| `--with-task` | `spec-to-rtl` | 也支持`code-complete-iccad2023` |
| `--with-samples` | `1` | 正式Pass@1配置 |
| `--with-examples` | `0` | Agent当前只支持零shot staging |
| `--with-agent-max-input-tokens` | `16384` | 每次请求可用输入上下文预算 |
| `--with-max-tokens` | `8192` | 每次模型调用输出上限 |
| `--with-temperature` | `0.6` | OpenCode采样参数 |
| `--with-top-p` | `0.95` | OpenCode采样参数 |
| `--with-agent-timeout` | `300` | 每个外部Agent进程的硬wall timeout |
| `--with-agent-max-turns` | `20` | 回合硬预算；Pi由宿主事件流终止，OpenCode映射为steps |
| `--with-agent-max-tool-calls` | `50` | 完成工具调用硬预算，由宿主事件流终止 |
| `--with-agent-thinking` | `on` | Qwen request-level thinking开关 |
| `--with-agent-tool-profile` | `base` | `base`或`rtl`镜像 |

`--with-agent-thinking=off`只允许Qwen模型。OpenCode通过模型配置中的
`chat_template_kwargs.enable_thinking`控制；Pi使用其Qwen thinking兼容格式。
不修改正在运行的vLLM部署。Driver把输入预算与输出上限相加，因此默认向
Agent声明`24576`总context；OpenCode在约`16384`输入处压缩，Pi使用显式
`reserveTokens=8192`和`keepRecentTokens=8192`实现相同阈值。

并发由宿主环境控制：

```bash
export VERILOG_EVAL_JOBS=8
```

每个Make generation目标仍对应一个独立Agent container。

## 4. 公开workspace与正式提交

`spec-to-rtl`初始workspace：

```text
/workspace/
├── TASK.md
├── RULES.md          # 仅显式启用时
└── .agent-config/
```

`code-complete-iccad2023`还会包含公开starter：

```text
/workspace/TopModule.sv
```

不会进入workspace或任何container mount的内容：

```text
*_ref.sv
*_test.sv
dataset目录
build目录
 prior summary/trajectory
宿主repository
Docker socket
无关宿主环境变量或secret
```

Agent结束后只检查`/workspace/TopModule.sv`。发布器拒绝：

- 缺失文件；
- symlink；
- 非regular file；
- 空文件；
- 超过大小上限的文件；
- 未修改的code-completion starter。

无有效artifact时会发布确定性无效占位文件，使Make可以保留完整分母。
它不会从聊天中恢复代码。

## 5. 外部Agent Driver

Driver只负责写外部CLI配置、构造argv和解析机器事件：

```text
agent_generation/drivers/pi.py
agent_generation/drivers/opencode.py
```

已验证的官方工具版本：

```text
Pi       0.82.1
OpenCode 1.18.7
```

评测不会自动安装它们。`AGENT_EVAL_AGENT_TOOLS`可以指向官方安装，也可以
指向改过源码后构建出的prefix。prefix必须把所选CLI放在：

```text
<tools>/node_modules/.bin/pi
<tools>/node_modules/.bin/opencode
```

只要求本次所选Agent对应的入口存在。每次run会记录完整目录内容摘要；路径和
mtime不影响摘要，文件内容、可执行位和symlink目标会影响摘要。逃逸prefix的
symlink会被拒绝。

当前profile：

```text
pi-inline-artifact-thinking-v1
pi-inline-artifact-no-thinking-v1
opencode-artifact-thinking-v2
opencode-artifact-no-thinking-v2
```

Pi会把与`TASK.md`相同的公开题目正文直接放入初始prompt，避免把“先调用
read才能看到题目”作为成功前提。系统提示仍明确要求调用`write`或`edit`
创建artifact，并显式配置输入context压缩预算；它不允许将聊天代码转换为
提交。所有命令均以argv执行，不经过宿主shell插值。API key只以
显式允许的环境变量传入container，不写入配置、manifest或命令行。

## 6. Docker隔离

每个sample只挂载：

```text
/workspace   # 当前sample，可写
/agent-tools # 固定Agent tools，只读
```

Docker策略：

```text
read-only root filesystem
non-root UID/GID
cap-drop=ALL
no-new-privileges
PID limit
memory limit
/tmp tmpfs with noexec,nosuid,nodev
no Docker socket
unique container name and cidfile
```

宿主以root运行时，workspace在启动前转交给`65534:65534`。超过wall
 timeout后，executor执行`docker rm --force`并在返回前确认清理成功。Pi的
`turn_end`以及两种Agent的完成工具事件由宿主实时计数；达到预算时同样强制
删除container，分别记录`max_turns`或`max_tool_calls`终止原因。

## 7. 输出

每个配置位于独立build目录：

```text
build/agent-nix-eval-<hash>/
├── summary.csv
├── summary.txt
├── agent-summary.json
├── agent-summary.txt
└── <problem>/
    ├── <problem>_sample01.sv
    ├── <problem>_sample01-sv-generate.log
    ├── <problem>_sample01-sv-iv-test.log
    ├── <problem>_sample01-generation.json
    ├── <problem>_sample01-trajectory.jsonl
    └── <problem>_sample01-stderr.log
```

`*-generation.json`使用`agent-generation/v1`，分别包含：

```text
producer
execution.status / exit_code / duration_seconds / termination_reason
limits.timeout_seconds / max_turns / max_tool_calls
limits.max_input_tokens / per_call_max_tokens
submission.status / sha256 / size_bytes
usage.input_tokens / output_tokens / turns / tool_calls / usage_source
runtime.source_revision / source_diff_sha256 / docker_image_id
runtime.agent_tools_versions / agent_tools_lock_sha256 / agent_tools_content_sha256
runtime.api_base_url
```

典型正交组合：

```text
execution=completed submission=published correctness=pass
execution=completed submission=missing   correctness=fail
execution=timeout   submission=published correctness=pass-or-fail
execution=error     submission=missing   correctness=fail
```

`agent-summary.json`使用`agent-evaluation/v1`，分别汇总：

```text
correctness
execution
submission
usage
samples[]
```

有任何sample缺少token usage时，汇总`value`为`null`，同时保留
`known_sum`、`known_samples`和`unknown_samples`。不会把unknown伪造成0。
`summary.txt`中的`total_cost`是上游分析器要求的兼容数字栏；Agent遥测应以
manifest和`agent-summary.json`为准。

## 8. 正式验证门

本地单元与Nix检查：

```bash
python3 -m unittest discover -s tests -v
nix flake check --all-systems --no-build "path:$PWD"
```

真实Docker隐藏数据哨兵：

```bash
tests/integration/agent-docker-isolation-smoke
```

该测试使用假Agent主动搜索隐藏grader文件、宿主repo、Docker socket和未授权
环境变量。它不调用模型。

真实Docker预算终止与超时清理：

```bash
tests/integration/agent-docker-budget-smoke
tests/integration/agent-docker-timeout-smoke
```

该测试先写candidate再故意sleep，要求最终同时满足：

```text
execution.status=timeout
execution.exit_code=124
submission.status=published
Icarus pass
无残留container
```

以上两项通过后，再分别运行一个真实OpenCode和Pi单题smoke。完成这些门之前
不要启动完整156题评测。

## 9. 结果解释

- temperature 0.6加单样本具有随机性；单次agent或profile差异不是稳定质量证据。
- Pass@1允许Agent内部多轮及工具调用，但不允许隐藏grader反馈或grader后重试。
- `Error 2 (ignored)`是原Make规则保留完整分母的行为，应读取最终summary与
  manifest确认原因。
- build hash由配置参数决定。要排除旧artifact复用，使用新的
  `VERILOG_EVAL_BUILD_ROOT`。
- 生成正确代码但只打印到聊天时，结果必须是`submission=missing`。

## 10. 常见诊断

查看单题manifest：

```bash
manifest=$(find build -name '*-generation.json' -print -quit)
python3 -m json.tool "$manifest"
```

检查残留container：

```bash
docker ps -a --format '{{.Names}}' | grep '^verilog-eval-' || echo 'no leftovers'
```

检查最终Agent报告：

```bash
python3 -m json.tool build/agent-nix-eval-*/agent-summary.json
```

所有正式run应保留canonical summaries、candidate、generation manifest、轨迹、
stderr和Agent汇总报告。
