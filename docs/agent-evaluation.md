# Agent 评测指南

## 1. 核心边界

Agent 只替换 VerilogEval 的代码生成命令：

```text
公开 benchmark 输入
  → Agent generator backend
  → 标准 sample.sv + generation log
  → 原版 Makefile / iverilog / hidden tests / sv-iv-analyze
```

架构图：[`agent-eval-generator-architecture.excalidraw`](agent-eval-generator-architecture.excalidraw)

Agent generator 实现与 `scripts/sv-generate` 相同的关键合同：

```text
输入：task、prompt 文件、model/sampling 参数、output 路径
输出：完整 Verilog candidate、generation log
```

Adapter 不进行提示词优化，不教工具序列化格式，不抽取聊天代码，不重试缺失提交，也不参与正确性评分。

## 2. 两种任务输入

### `spec-to-rtl`

Agent 工作区包含：

```text
TASK.md       # 原始 *_prompt.txt
```

Agent 必须创建：

```text
TopModule.sv
```

### `code-complete-iccad2023`

Agent 工作区包含：

```text
TASK.md       # 原始 *_prompt.txt
TopModule.sv  # 使用公开 *_ifc.txt 初始化
```

Agent 必须完成并修改 `TopModule.sv`。未修改的 starter 视为 `missing_submission`。

两种任务都不会向 Agent 暴露：

```text
*_ref.sv
*_test.sv
```

## 3. Generator 与 Backend 职责

`agent_eval/generate.py` 负责一次 Make generation request：

1. 创建该 sample 的隔离工作区；
2. 放入公开 prompt/starter；
3. 调用 `agent_eval/backend.py` 生成 Pi 或 OpenCode CLI 命令；
4. 在 Bubblewrap 或 Docker 中执行 Agent；
5. 将 `TopModule.sv` 发布到 Make 指定的 sample 路径；
6. 写入 token、turn、tool、状态和轨迹 sidecar。

`agent_eval/backend.py` 是薄转换层，只处理 Pi/OpenCode 命令行差异。

OpenCode 使用专用 `benchmark` primary Agent，只开放 read/edit/bash权限，并将 workspace 文件定义为正式 deliverable。该 profile 不包含工具序列化语法或 Verilog 解题提示。默认 profile 为 `opencode-artifact-v4`；加载 inline-skill Digital Chip Design Agents Harness 时为 `opencode-dcda-inline-v1`；Pi 为 `pi-standard-v3`。每个 workspace 只包含当前 Agent 的配置。`agent.json` 和 generation log 会记录 `adapter_profile`，避免把不同 Adapter/Harness 结果混合。

如果 Agent 没有产生有效 artifact，generator 会写入明确的失败占位样本，让完整 Make 批次继续，并保留真实状态：

```text
missing_submission
agent_error
timeout
```

正式 Pass@1 不自动重试。

## 4. 运行

推荐使用包装脚本，使 Nix、npm、XDG 和 Agent 缓存保留在仓库 `.cache`：

```bash
cd /opt/agent/verilog-eval
./scripts/agent-eval \
  --agent opencode \
  --with-task=spec-to-rtl \
  --with-model=qwen3.6-coder \
  --with-samples=1 \
  --with-max-tokens=8192 \
  --with-temperature=0.6 \
  --with-top-p=0.95 \
  --jobs 48 \
  --timeout 180
```

Pi：

```bash
./scripts/agent-eval \
  --agent pi \
  --with-task=spec-to-rtl \
  --jobs 48
```

全部题目是默认行为。单题或小批量：

```bash
./scripts/agent-eval \
  --agent opencode \
  --with-task=spec-to-rtl \
  --problems Prob001_zero Prob002_m2014_q4i \
  --jobs 2
```

使用与 model-only 相同的问题列表文件：

```bash
./scripts/agent-eval \
  --agent opencode \
  --with-task=spec-to-rtl \
  --with-problems=/path/to/problems.txt \
  --jobs 48
```

使用本地构建的 Agent tools prefix：

```bash
./scripts/agent-eval \
  --agent opencode \
  --agent-tools=/opt/agent-tools-local/opencode \
  --agent-source=/opt/src/opencode \
  --problems Prob001_zero \
  --run-root=runs/opencode-local-smoke
```

`--agent-tools` 目录必须包含 `node_modules/.bin/<agent>`，且内部 symlink 不得逃逸该目录。Runner 会先将完整 tools prefix 复制到 run 目录并校验 content digest；后续所有 sample 只读挂载该冻结快照。`--agent-source` 只用于记录 Git commit、dirty patch、untracked archive 和 lockfile digest，不会挂载进 Agent 容器。本地覆盖一次只能运行一个明确的 `--agent`。

加载 inline-skill OpenCode Harness：

```bash
./scripts/agent-eval \
  --agent opencode \
  --opencode-harness=/opt/agent/digital-chip-design-agents \
  --problems Prob001_zero \
  --timeout 300 \
  --run-root=runs/opencode-dcda-smoke
```

Runner 只复制该 Git工作树中 tracked 和非 ignored 的 untracked文件，拒绝逃逸 Harness 根目录的 symlink，并将冻结快照只读挂载到 `/opencode-harness`。Harness `opencode.json` 中的全部 `chip-*` Agent 会被加载；其 skills 已内联在 Agent prompt 中，因此 `skill` tool 继续禁用。`benchmark` 只能通过 `task` 调用 `chip-*`，不能调用其他 subagent。

## 5. 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--agent` | `all` | `pi`、`opencode` 或依次运行两者 |
| `--with-task` | `spec-to-rtl` | benchmark task |
| `--with-model` | `qwen3.6-coder` | 本地模型 ID |
| `--with-samples` | `1` | 当前正式 Agent 评测固定为 Pass@1 |
| `--with-max-tokens` | `8192` | 每次模型响应上限 |
| `--with-temperature` | `0.6` | OpenCode 请求参数与运行元数据 |
| `--with-top-p` | `0.95` | OpenCode 请求参数与运行元数据 |
| `--jobs` | 最多 16 | 原版 Make 并发生成/评分任务数 |
| `--timeout` | `180` | 单个外部 Agent CLI 的秒数上限 |
| `--sandbox` | `auto` | `auto`、`bwrap` 或 `docker` |
| `--agent-tools` | 内置固定版本 | 本地构建的 Agent tools prefix |
| `--agent-source` | 未设置 | 与本地 tools 对应的 Git 源码目录 |
| `--opencode-harness` | 未设置 | inline-skill OpenCode Harness Git工作树 |
| `--run-root` | 自动时间戳 | 结果根目录 |
| `--dry-run` | 关闭 | 只写 configure/make 命令，不执行 |

旧写法 `--task`、`--model`、`--max-tokens`、`--temperature` 和 `--top-p` 仍是兼容别名。

Pi 0.82.1 没有公开 temperature/top-p CLI 参数，因此 Pi 使用 vLLM 服务端采样默认值。运行 Pi 时应让命令元数据与 vLLM `--override-generation-config` 一致。

## 6. 输出

```text
runs/agent-eval-<UTC>/
└── <agent>/
    ├── commands.json
    ├── agent-source.json       # 使用本地 tools 时
    ├── agent-source.patch      # 本地源码有 tracked 修改时
    ├── agent-tools-snapshot/      # 冻结的本地构建产物
    ├── opencode-harness.json      # Harness commit/digest/Agent清单
    ├── opencode-harness-snapshot/ # 冻结的 Harness工作树
    ├── configure.log
    ├── make.log
    ├── summary.csv
    ├── summary.txt
    ├── verilog-eval/
    │   ├── summary.csv
    │   ├── summary.txt
    │   └── <problem>/
    │       ├── <problem>_sample01.sv
    │       ├── <problem>_sample01-sv-generate.log
    │       └── <problem>_sample01-sv-iv-test.log
    └── <problem>/
        └── sample01/
            ├── agent.json
            ├── command.json
            ├── trajectory.jsonl
            ├── stderr.log
            └── workspace/
                ├── TASK.md
                └── TopModule.sv
```

正确性只读取原版：

```text
<agent>/summary.csv
<agent>/summary.txt
```

Agent 行为读取：

```text
<agent>/<problem>/sample01/agent.json
<agent>/<problem>/sample01/trajectory.jsonl
```

`timeout` 时如果已经写出 `TopModule.sv`，candidate 仍会进入原版评分，同时 `agent.json` 保留 `status=timeout`。

生成汇总统计：

```bash
./scripts/agent-eval-stats runs/agent-eval-<UTC>
./scripts/agent-eval-stats --json runs/agent-eval-<UTC> > stats.json
```

## 7. 隔离

Agent 沙箱只挂载该 sample 的 `/workspace` 和只读 Agent 工具目录。隐藏数据集不挂载。Docker 使用：

```text
read-only root
non-root UID/GID
cap-drop ALL
no-new-privileges
PID limit
no Docker socket
```

每次运行开始时都会重新加载 Nix 生成的固定镜像 archive，并把解析后的 Docker image ID 写入 `agent.json`。若宿主进程 UID 为 0，容器会映射到 `65534:65534`，单题 workspace 在启动前转移给该用户。每个容器使用独立 `container.cid`；Agent timeout 后 generator 会执行 `docker rm --force`，防止遗留容器继续占用 vLLM。

Bubblewrap 只挂载选定 Nix store closure、动态加载器、Agent 工具和当前工作区。若宿主禁止 unprivileged user namespaces，`--sandbox auto` 会在启动任何 generation request 前回退到 Docker。

## 8. 旧结果

重构前通过独立 runner 生成、再用 `--with-pregen` 导回的运行应标记为：

```text
harness-v1 / protocol-prompted
```

新 Generator ABI 运行应标记为：

```text
harness-v2 / neutral-generator-backend
```

两种结果不能混在同一个实验配置中。
