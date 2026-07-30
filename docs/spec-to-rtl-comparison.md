# `spec-to-rtl` Evaluation Comparison

## Evaluation configuration

Unless noted otherwise, the Agent runs use the following configuration:

```text
model       = qwen3.6-coder
task        = spec-to-rtl
samples     = 1
max_tokens  = 8192
temperature = 0.6
top_p       = 0.95
jobs        = 48
sandbox     = docker
```

## Results

| Backend | Timeout | Pass@1 | Submission rate | Conditional pass rate | Timeouts |
| --- | ---: | ---: | ---: | ---: | ---: |
| Model-only | N/A | approximately **69%** | approximately 100% | approximately 69% | N/A |
| Pi (`pi-standard-v2`) | 180s | 102/156 = 65.38% | 143/156 = 91.67% | 102/143 = 71.33% | 29 |
| OpenCode (`opencode-artifact-v3`) | 180s | 87/156 = 55.77% | 129/156 = 82.69% | 87/129 = 67.44% | 73 |
| Pi (`pi-standard-v2`) | 300s | **103/156 = 66.03%** | **148/156 = 94.87%** | 103/148 = 69.59% | **5** |
| OpenCode (`opencode-artifact-v3`) | 300s | **103/156 = 66.03%** | 145/156 = 92.95% | **103/145 = 71.03%** | 12 |
| OpenCode DCDA (`opencode-dcda-chip-rtl-no-thinking-v1`) | 600s | **114/156 = 73.08%** | **156/156 = 100.00%** | **114/156 = 73.08%** | **2** |

The model-only result is currently recorded as an approximate value. It should not be presented as an exact reproduced result until its canonical summary and complete run configuration are attached.

## 300-second Agent comparison

| Metric | Pi | OpenCode |
| --- | ---: | ---: |
| Pass@1 | 103/156 | 103/156 |
| Submitted | 148/156 | 145/156 |
| Completed passes | 102/144 | 98/136 |
| Timeout submissions | 4/5 | 9/12 |
| Passes among timeout submissions | 1/4 | 5/9 |
| Turns | 381 | 398 |
| Tool calls | 233 | 254 |
| Total input tokens | 1,077,486 | 1,637,245 |
| Total output tokens | 279,423 | 314,355 |
| Total tokens | 1,356,909 | 1,951,600 |
| Average tokens per sample | 8,698.135 | 12,510.256 |
| Average duration per sample | 102.570s | 162.110s |
| Parse errors | 0 | 0 |

At 300 seconds, Pi and OpenCode have the same overall Pass@1. Pi has the higher submission rate, while OpenCode has the higher conditional pass rate. OpenCode uses approximately 43.8% more total tokens and has approximately 58.0% higher average duration.

## Run provenance

```text
Pi, 180 seconds:
/opt/agent/verilog-eval/runs/agent-eval-20260728T092112Z

OpenCode v3, 180 seconds:
/opt/agent/verilog-eval/runs/opencode-artifact-v3-full-20260729T015447Z

Pi, 300 seconds:
/opt/agent/verilog-eval/runs/pi-timeout300-full-20260729T023030Z

OpenCode v3, 300 seconds:
/opt/agent/verilog-eval/runs/opencode-artifact-v3-timeout300-full-20260729T025208Z
```

Each run directory should retain:

```text
commands.json
summary.csv
summary.txt
stats.txt
git-commit.txt
dirty-tree.patch
```

## Interpretation notes

- Overall Agent Pass@1 equals submission rate multiplied by conditional pass rate.
- Model-only generation converts the model response directly into a candidate, while Agent evaluation requires a workspace `TopModule.sv` artifact.
- Completed-task correctness is close to the model-only rate; most of the overall Agent loss comes from missing or incomplete submissions.
- OpenCode's 180-second result is strongly affected by timeout pressure. Increasing the timeout to 300 seconds raises its Pass@1 from 55.77% to 66.03%.
- Pi's Pass@1 changes from 65.38% to 66.03% when increasing the timeout to 300 seconds.
- Temperature is non-zero, so the 180-second and 300-second runs are independent Pass@1 samples, not deterministic continuations or retries.
- The obsolete OpenCode result produced before `opencode-artifact-v3` is excluded because its Adapter failed the artifact-submission contract.

## DCDA complex-5 exploratory matrix (A–H)

### Scope

These eight runs use the same five deliberately difficult public `spec-to-rtl` prompts:

```text
Prob144_conwaylife
Prob153_gshare
Prob154_fsm_ps2data
Prob155_lemmings4
Prob156_review2015_fancytimer
```

The frozen list is `problem-sets/spec-to-rtl-complex-5.txt`. Selection used public prompt length and behavioral complexity only; no `*_ref.sv` or `*_test.sv` file was inspected. Every run used:

```text
model       = qwen3.6-coder
temperature = 0.6
top_p       = 0.95
samples     = 1
jobs        = 5
sandbox     = docker
toolchain   = minimal-rtl
harness     = digital-chip-design-agents inline OpenCode config
```

`max_tokens` is the per-LLM-step output limit. Durations below are sums of the five per-sample Agent durations, not wall-clock batch time. These are independent stochastic Pass@1 runs, not retries from which a best candidate was selected.

### Run conditions

| ID | Primary | Qwen thinking | Max tokens | Timeout | Adapter profile |
| --- | --- | --- | ---: | ---: | --- |
| A | `benchmark` | on | 8,192 | 600s | `opencode-dcda-inline-v1` |
| B | `chip-rtl` | on | 8,192 | 600s | `opencode-dcda-chip-rtl-v1` |
| C | `benchmark` | on | 16,384 | 600s | `opencode-dcda-inline-v1` |
| D | `chip-rtl` | on | 16,384 | 600s | `opencode-dcda-chip-rtl-v1` |
| E | `benchmark` | on | 65,536 | 1,800s | `opencode-dcda-inline-v1` |
| F | `chip-rtl` | on | 65,536 | 1,800s | `opencode-dcda-chip-rtl-v1` |
| G | `benchmark` | off | 16,384 | 600s | `opencode-dcda-inline-no-thinking-v1` |
| H | `chip-rtl` | off | 16,384 | 600s | `opencode-dcda-chip-rtl-no-thinking-v1` |

Thinking-off runs send `chat_template_kwargs.enable_thinking=false` to vLLM. OpenCode CLI `--thinking` only controls whether reasoning blocks are displayed, so it remains enabled for observability and is not the generation-mode switch. G and H each recorded zero reasoning events, confirming the request-level switch worked.

### Aggregate results

| ID | Pass@1 | Submitted | Conditional pass | Missing | Status timeouts | Σ duration | Turns | Tool calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 0/5 (0%) | 3/5 (60%) | 0/3 (0%) | 2 | 0 | 699.944s | 12 | 7 |
| B | **3/5 (60%)** | 3/5 (60%) | **3/3 (100%)** | 2 | 0 | 662.195s | 10 | 5 |
| C | 2/5 (40%) | 4/5 (80%) | 2/4 (50%) | 1 | 0 | 1,016.804s | 13 | 8 |
| D | 0/5 (0%) | 2/5 (40%) | 0/2 (0%) | 3 | 0 | 1,131.242s | 9 | 4 |
| E | 1/5 (20%) | 5/5 (100%) | 1/5 (20%) | 0 | 0 | 1,149.845s | 16 | 11 |
| F | 1/5 (20%) | 5/5 (100%) | 1/5 (20%) | 0 | 0 | 1,314.581s | 20 | 15 |
| G | 2/5 (40%) | 5/5 (100%) | 2/5 (40%) | 0 | 0 | **440.494s** | 26 | 21 |
| H | 2/5 (40%) | 5/5 (100%) | 2/5 (40%) | 0 | 0 | 1,325.350s | 99 | 96 |

`missing_submission` means the external Agent process completed but `/workspace/TopModule.sv` did not exist. It is separate from timeout, Agent error, and Verilog correctness. Inspected 16K missing samples ended with `finish_reason=length`, exactly 16,384 output tokens, and zero tool calls. At 64K, both primary choices reached 100% submission.

### Token totals

| ID | Input tokens | Output tokens | Total tokens | Average total/sample |
| --- | ---: | ---: | ---: | ---: |
| A | 106,305 | 38,220 | 144,525 | 28,905.0 |
| B | 153,276 | 34,541 | 187,817 | 37,563.4 |
| C | 123,427 | 50,837 | 174,264 | 34,852.8 |
| D | 135,332 | 59,457 | 194,789 | 38,957.8 |
| E | 210,442 | 60,763 | 271,205 | 54,241.0 |
| F | 444,983 | 69,752 | 514,735 | 102,947.0 |
| G | 340,033 | 23,977 | 364,010 | 72,802.0 |
| H | 2,551,686 | 71,163 | 2,622,849 | 524,569.8 |

At 16K with thinking disabled, H and G have the same Pass@1 and submission rate, while H uses 7.2× as many total tokens, 7.5× as many input tokens, 4.6× as many root-session tool calls, and 3.0× the summed sample duration.

### Tool and Harness behavior

| Run | Observed root-session behavior |
| --- | --- |
| A | Three `read` and four `write` calls; no subagent or EDA command |
| B | Two `read` and three `write` calls; no subagent or EDA command |
| C | Four `read` and four `write` calls; no subagent or EDA command |
| D | Two `read` and two `write` calls; no subagent or EDA command |
| E | Five `read`, five `write`, and one `task(chip-verification)` call |
| F | Used Verilator lint on FancyTimer, edited the candidate, and linted again; no subagent call |
| G | Ten `bash`, four `read`, and seven `write` calls; no reasoning events |
| H | 48 `bash`, two `edit`, eight `read`, four `todowrite`, and 34 `write` calls; no reasoning events |

H proved that the Docker toolchain can execute Icarus Verilog/`vvp`, Verilator, Slang, and Surelog. It did not exercise Yosys, ABC, SymbiYosys, or sv2v. Its flow generated testbenches and performed lint/compile/simulation loops, but also repeated identical commands, tried invalid tool flags, and retried incorrect invocation forms. The extra tool activity did not improve aggregate Pass@1 over G.

Root `trajectory.jsonl` records the primary session and the parent `task` result, but not every internal child-session event. Consequently, E's nested `chip-verification` token and tool totals are undercounted until recursive child-session export is implemented.

### Confirmed multi-Agent failure: `Prob154_fsm_ps2data`

In E, the natural primary called `chip-verification` before writing `TopModule.sv`. The parent supplied a leading request that claimed `0x0d` had `in[3]=0` and asked the child to confirm an unconditional transition. The child confirmed it without independently checking the bit value or reviewing a final artifact.

The arithmetic is:

```text
0x0d = 8'b0000_1101
in[3] = 1
```

All bytes shown at a `done` cycle in the public waveform (`6b`, `6d`, `0d`, `ed`, `ce`) have bit 3 set. The waveform therefore does not distinguish conditional from unconditional capture. The generated candidate nevertheless encoded:

```systemverilog
GOT3: begin
    byte1 <= in;
    next_state = GOT1;
end
```

It should check `in[3]` and return to `IDLE` when the current byte is not a valid boundary. Original VerilogEval grading rejected the candidate. This is a concrete confirmation-bias failure: the child Agent amplified a false parent premise rather than independently validating the public specification and completed RTL.

### Interpretation

- The best observed raw score is B at 3/5, but B submits only 60%, and the direction reverses at 16K. There is no stable evidence across A–H that explicit `chip-rtl` improves Pass@1.
- Raising the thinking-on output limit to 64K eliminates missing submissions in this subset, but both primaries score only 1/5. More output budget improves artifact reliability, not demonstrated correctness.
- G is the most efficient 100%-submission condition in this matrix: 2/5 Pass@1 and 440.494 summed seconds. It does not invoke a `chip-*` subagent, so it is an OpenCode-plus-EDA baseline rather than evidence of DCDA routing benefit.
- H is the clearest execution of the native `chip-rtl` tool workflow, but it reaches the same 2/5 score as G at 7.2× total tokens. It is not ready for a cost-effective full 156-problem run.
- These five hand-selected hard prompts and non-zero-temperature single samples are exploratory. They are suitable for finding integration failures, not for a statistically reliable model or Harness ranking.

### Run provenance

```text
A: /opt/agent/verilog-eval/runs/complex5-natural-20260729T081332Z
B: /opt/agent/verilog-eval/runs/complex5-chip-rtl-20260729T081332Z
C: /opt/agent/verilog-eval/runs/complex5-natural-mt16384-20260729T083718Z
D: /opt/agent/verilog-eval/runs/complex5-chip-rtl-mt16384-20260729T083718Z
E: /opt/agent/verilog-eval/runs/complex5-natural-thinking-mt65536-20260729T090617Z
F: /opt/agent/verilog-eval/runs/complex5-chip-rtl-thinking-mt65536-20260729T090617Z
G: /opt/agent/verilog-eval/runs/complex5-natural-no-thinking-mt16384-20260729T095050Z
H: /opt/agent/verilog-eval/runs/complex5-chip-rtl-no-thinking-mt16384-20260729T095050Z
```

Evaluator commits:

```text
A–F: 0427837 (explicit OpenCode primary comparison support)
G–H: 2697f50 (request-level Qwen thinking control)
```

B's first command was interrupted before the evaluation-start banner and then rerun into the same named directory. The completed run contains all five sidecars and canonical summaries, but the matrix remains labeled exploratory.

## Full `chip-rtl` no-thinking run (I)

After A–H, the H configuration was run across all 156 `spec-to-rtl` problems:

```text
primary      = chip-rtl
thinking     = off
max_tokens   = 16,384
timeout       = 600s
jobs          = 48
toolchain     = minimal-rtl
temperature   = 0.6
top_p         = 0.95
samples       = 1
profile       = opencode-dcda-chip-rtl-no-thinking-v1
```

### Result

| Metric | I |
| --- | ---: |
| Pass@1 | **114/156 = 73.08%** |
| Submitted | **156/156 = 100.00%** |
| Conditional pass rate | 114/156 = 73.08% |
| Completed | 154 |
| Agent timeouts | 2 |
| Submitted at timeout | 2/2 |
| Passed at timeout | 1/2 |
| Sidecars | 156/156 |
| Parse errors | 0 |

VerilogEval symbols:

```text
. = 114
C =   2
R =  25
S =   6
T =   3
e =   1
p =   1
w =   4
```

Agent timeout status and VerilogEval's `T` symbol are separate dimensions. Both timed-out Agent samples still contained publishable artifacts, and one passed original grading.

### Resource use

| Metric | Total | Average/sample |
| --- | ---: | ---: |
| Duration | 21,179.737s | 135.768s |
| Turns | 670 | 4.295 |
| Tool calls | 533 | 3.417 |
| Input tokens | 10,281,056 | 65,904.205 |
| Output tokens | 274,178 | 1,757.551 |
| Total tokens | 10,555,234 | 67,661.756 |

The 156 samples ran concurrently, so 21,179.737 seconds is the sum of sample durations rather than elapsed wall time.

### Comparison with the earlier full OpenCode run

| Metric | OpenCode v3, 300s | I | Difference |
| --- | ---: | ---: | ---: |
| Pass@1 | 103/156 (66.03%) | 114/156 (73.08%) | +11 passes, +7.05 points |
| Submitted | 145/156 (92.95%) | 156/156 (100%) | +11 submissions |
| Conditional pass rate | 71.03% | 73.08% | +2.05 points |
| Agent timeouts | 12 | 2 | -10 |
| Average duration | 162.110s | 135.768s | -16.2% |
| Tool calls | 254 | 533 | 2.10× |
| Total tokens | 1,951,600 | 10,555,234 | 5.41× |

This is not a matched causal comparison. I simultaneously changes the Adapter/Harness, Qwen thinking mode, output limit, timeout, and toolchain. Its higher score therefore cannot be attributed to `chip-rtl` alone. Temperature 0.6 also makes the two runs independent stochastic samples.

The complex-five subset inside I scores 1/5: ConwayLife passes, while gshare, PS/2 data, Lemmings4, and FancyTimer fail. H scored 2/5 on the same prompts. This within-configuration variation reinforces that one sample per task is insufficient for a stable efficacy estimate.

### Run provenance

```text
I: /opt/agent/verilog-eval/runs/agent-eval-20260729T102105Z
```

The run directory contains the canonical summary, all 156 generator sidecars, frozen Agent/Harness provenance, and per-sample trajectories. The exact evaluator and Harness commits recorded there are authoritative for reproducing I.
