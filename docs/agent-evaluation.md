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

### Complete DCD front-end producer

`pi-dcd-front-end` is an explicit completion-result-selected producer. The evaluation
call adapter invokes the DCD Extension command `/dcd-front-end` directly, so the parent
model does not decide whether to load a Skill. DCD then owns Architecture, independent
oracle planning, RTL, semantic EDA, and bounded repair.

Build a model-neutral DCD Pi bundle from the DCD repository, then provide it explicitly:

```bash
node /absolute/path/to/digital-chip-design-agents/bin/build-pi-bundle.mjs \
  --output /opt/agent/dcd-pi.tar

export AGENT_EVAL_DCD_PI_BUNDLE=/opt/agent/dcd-pi.tar
VERILOG_EVAL_JOBS=8 nix run .#agent-eval -- \
  --with-agent=pi-dcd-front-end \
  --with-model=qwen3.6-coder \
  --with-agent-max-input-tokens=32768 \
  --with-max-tokens=65536 \
  --with-agent-toolset=rtl \
  --with-problems=/absolute/path/to/problems.txt \
  --new-run --run-path-file=/absolute/path/to/run.path
```

The bundle is a bounded uncompressed tar containing exactly 16 DCD Agents, 17 Skills,
and six Extension files—no model settings. Its SHA-256 and size are material identity; its path is
an ephemeral runtime binding. Each sample verifies the bytes again, safely expands them
into writable `/workspace/.agent-config/pi`, and then writes the selected model config.
This is required because Pi acquires `settings.json.lock`.

The evaluator system prompt contains only the final artifact acceptance contract. Tool
and Orchestrator instructions come from DCD. Child Pi lifecycle entries are persisted as
`dcd_child_event` custom entries and unwrapped only for aggregate host turn/tool budgets
and usage accounting. DCD also uses authenticated domain-child completion and nested
Extension-issued EDA receipts for its completion gate. The sole submission remains `/workspace/TopModule.sv`; DCD writes
`.dcd/front-end-result.json` during execution and mirrors it as a persisted
`dcd_front_end_result` trajectory entry. Neither form is a submission.

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
- submission state from structural host-side `/workspace/TopModule.sv` inspection;
- correctness from the original Icarus grader.

Submission inspection is limited to path, file type, nonempty bounded size, and unchanged
public-starter detection. Candidate content belongs to the Agent and is published
byte-for-byte; it is never scanned for credentials or other policy. Diagnostic redaction
is a separate persistence operation and cannot affect submission status.

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
