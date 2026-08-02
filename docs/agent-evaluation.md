# Agent Evaluation

Agent evaluation keeps benchmark scheduling and correctness unchanged while replacing the
sample producer with Pi or OpenCode.

```text
Nix binding adapter
  → agent_generation.run
  → canonical content-addressed run
  → configure generic generator seam
  → GNU Make (only scheduler)
  → sv-invoke-generator → sv-agent-generate
  → fresh Docker workspace
  → candidate-last Sample Bundle
  → original Icarus grading
  → hash-bound Agent report
```

The normative interface and trust-boundary details are in
[`agent-generator-interface.md`](agent-generator-interface.md).

## Prerequisites

- clean tracked Git worktree;
- Linux with Docker and Nix;
- OpenAI-compatible endpoint advertising the exact model at `<base>/models`;
- explicit external npm-lock-backed tools prefix containing pinned Pi and OpenCode;
- API credential in an explicitly named environment variable.

The setup helper is intentionally outside the formal app:

```bash
nix run .#setup -- # Python/model-only setup only
nix run .#agent-tools-setup
```

For formal runs, provide a previously prepared tools prefix rather than invoking setup:

```bash
export AGENT_EVAL_AGENT_TOOLS=/opt/agent/verilog-eval/.agent-tools
export OPENAI_API_KEY='...'
```

## One-sample smoke

```bash
printf 'Prob001_zero\n' >/tmp/agent-smoke.txt
run_path_file="$(mktemp)"
chmod 0600 "$run_path_file"

VERILOG_EVAL_JOBS=1 nix run .#agent-eval -- \
  --with-agent=opencode \
  --with-model=qwen3.6-coder \
  --with-openai-api-base=http://127.0.0.1:58000/v1 \
  --with-problems=/tmp/agent-smoke.txt \
  --with-agent-max-input-tokens=16384 \
  --with-max-tokens=16384 \
  --with-agent-timeout=300 \
  --with-agent-thinking=on \
  --with-agent-toolset=standard \
  --run-path-file="$run_path_file"

scripts/validate-agent-run --expected-samples=1 "$(cat "$run_path_file")"
```

Change `--with-agent=pi` to run Pi. The default material values are:

| Input | Default |
|---|---:|
| Agent | `opencode` |
| Model | `qwen3.6-coder` |
| Task | `spec-to-rtl` |
| Samples/problem | `1` |
| Input budget | `16384` |
| Output budget | `16384` |
| Timeout/sample | `300` seconds |
| Turn budget | `20` |
| Tool-call budget | `50` |
| Thinking | `on` |
| Toolset | `standard` |
| Make jobs | `VERILOG_EVAL_JOBS`, default `4` |

The Agent interface intentionally has no sampling controls. Thinking, context/output
limits, host turn/tool budgets, and toolset are material run identity.

### Focused DCD RTL-module producer

`pi-dcd-rtl-module` is an explicit Pi producer rather than a prompt-selected mode. It
requires a supplied sandbox image containing the fixed `/dcd-dispatch` executable and
its DCD resources. The driver invokes that executable with `--entry rtl-module`; a base
image without the dispatcher fails closed instead of silently running ordinary Pi.

```bash
VERILOG_EVAL_JOBS=8 nix run .#agent-eval -- \
  --with-agent=pi-dcd-rtl-module \
  --with-model=qwen3.6-coder \
  --with-agent-max-input-tokens=32768 \
  --with-max-tokens=32768 \
  --with-agent-toolset=rtl \
  --docker-image=verilog-eval-agent-sandbox:rtl-dcd-module \
  --docker-archive=/absolute/path/to/rtl-dcd-module.tar \
  --with-problems=/absolute/path/to/problems.txt \
  --new-run --run-path-file=/absolute/path/to/run.path
```

The producer name, image content ID, prompt inputs, and limits are material identity.
Each trajectory starts with `dcd_dispatch`, followed by the selected Pi process's normal
JSON events; no parent model call occurs before dispatch. The sole submission remains
`/workspace/TopModule.sv`.

## Formal run

Create an explicit problem file or use the dataset default. A 156-sample run at concurrency
16 is:

```bash
run_path_file="$(mktemp)"
chmod 0600 "$run_path_file"

VERILOG_EVAL_JOBS=16 nix run .#agent-eval -- \
  --with-agent=pi \
  --with-model=qwen3.6-coder \
  --with-samples=1 \
  --with-agent-max-input-tokens=16384 \
  --with-max-tokens=16384 \
  --with-agent-thinking=on \
  --with-agent-toolset=standard \
  --run-path-file="$run_path_file"

run_dir="$(cat "$run_path_file")"
scripts/validate-agent-run --expected-samples=156 "$run_dir"
```

Use the same command with `--with-agent=opencode` for the second formal run. Identical
material config resumes the same content-addressed directory. Use `--new-run` only when an
intentional independent repetition is required; the nonce and recovery receipt become
part of the audit trail.

## Run layout

A completed run is named by the SHA-256 of exact canonical `run-config.json` bytes:

```text
<build-root>/<64-hex-config-digest>/
├── run-config.json
├── endpoint-evidence.json
├── .generator-args
├── summary.csv
├── summary.txt
├── agent-summary.txt
├── agent-summary.json       # completion marker
└── <problem>/
    ├── <sample>.sv           # Sample Bundle completion marker
    ├── <sample>-generation.json
    ├── <sample>-trajectory.jsonl
    ├── <sample>-stderr.log
    ├── <sample>-sv-generate.log
    └── <sample>-sv-iv-test.log
```

`runtime-bindings.json`, `.credential.sock`, temporary workspaces, and configure HOME are
runtime-only and must be absent from a completed run. An Icarus executable may be absent
when compilation failed; that is ordinary correctness evidence, not infrastructure failure.

`agent-summary.json` reports three separate axes:

- execution outcome from the Agent process;
- submission state from host-side `/workspace/TopModule.sv` inspection;
- correctness from the original Icarus grader.

Unknown token/turn/tool usage remains `null` with `usage_source=unavailable`; aggregate
reports retain known sums and unknown-sample counts rather than inventing zeros.

## Resume and recovery

Ordinary reruns validate current endpoint, source, image, tools, and host identities before
resuming. Hash-valid committed samples are reused. A valid completed report returns without
Make.

For nonce runs interrupted between config publication and path acknowledgement:

```bash
scripts/run-agent-evaluation --build-root "$VERILOG_EVAL_BUILD_ROOT" --list-recoveries
scripts/run-agent-evaluation --build-root "$VERILOG_EVAL_BUILD_ROOT" \
  --resume-recovery <digest> --run-path-file /tmp/recovered-run
scripts/run-agent-evaluation --build-root "$VERILOG_EVAL_BUILD_ROOT" \
  --abandon-recovery <digest>
```

Abandonment quarantines the entire incomplete run; it does not delete individual sidecars.
Suspicious evidence is likewise quarantined only under the exclusive lifecycle lock after
live processes, containers, broker state, and run locks are ruled out.

## Failure interpretation

- No candidate and regular sidecars: recover and regenerate the sample.
- Candidate plus invalid/missing/hash-mismatched sidecar: infrastructure corruption;
  quarantine the run.
- Agent timeout/budget/process error with a committed frozen placeholder: valid sample
  evidence; grading should fail normally.
- Docker control failure, unsafe filesystem entry, identity mismatch, broker cleanup
  failure, nonzero Make, or report transaction failure: no completed report.
- Queue-inclusive timeout pressure at concurrency above endpoint capacity is a deployment
  effect, not a protocol success.

Do not recover Verilog from chat, stdout, trajectory, logs, or previous results. The only
formal submission is the candidate committed from `/workspace/TopModule.sv`.
