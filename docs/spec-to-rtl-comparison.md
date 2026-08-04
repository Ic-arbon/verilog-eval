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

### Root tool-call audit

The 533 root-session tool calls break down as follows:

| Tool | Calls |
| --- | ---: |
| `bash` | 206 |
| `write` | 202 |
| `read` | 91 |
| `todowrite` | 15 |
| `glob` | 10 |
| `edit` | 8 |
| `invalid` | 1 |
| `task` | 0 |

Only 20/156 samples (12.82%) contain EDA-related shell lines, totaling 110 extracted lines. These lines include real compile/lint/simulation/synthesis commands as well as discovery, version, and help commands; they must not all be counted as successful validation. Actual root-session invocation was observed for Icarus Verilog/`vvp`, Verilator, Slang, Yosys, and sv2v. Surelog was discovered with `which` but not executed; ABC and SymbiYosys were not executed.

The 20 EDA-active problems score 13/20 (65%). The other 136 problems score 101/136 (74.26%). This is not evidence that EDA reduces correctness: the Agent selects tools non-randomly for harder tasks, so task difficulty confounds the comparison.

The audit found repeated or ineffective commands, including four identical Icarus compile/simulation calls for `Prob131_mt2015_q4`, repeated Verilator lint calls, multiple Slang option forms followed by `slang --help` and a bare invocation, and compile failures masked with `|| true`. `Prob149_ece241_2013_q4` performed many generated-testbench simulations but still failed original grading.

`Prob030_popcount255` provides a concrete false-test example. Its generated testbench expected 255 set bits from `255'h7FFFFFFFF`, which contains only 35 set bits, and expected 127 from `255'h7FFFFFFFFFFFFFFFF`, which contains only 67. The safe forms are `{255{1'b1}}` and `{{128{1'b0}}, {127{1'b1}}}`. Separately, the trajectory contained a malformed `</parameter` shell fragment and one tool call was classified as `invalid`.

Exact root-session inputs are retained under the run directory:

```text
opencode/tool-call-report/root-tool-calls.jsonl
opencode/tool-call-report/bash-commands.txt
opencode/tool-call-report/eda-commands.txt
opencode/tool-call-report/eda-invocations.tsv
```

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

## 2026-08-03 full four-arm rerun

### Scope and frozen configuration

This experiment reran all 156 `spec-to-rtl` problems once in each of four arms. The arm invocations were strictly serial in this order:

```text
bare model → OpenCode clean → Pi clean → Pi extensions
```

All arms used the same prompt bytes, the same served `qwen3.6-coder` model from `/opt/llm/Qwen3.6-27B`, eight concurrent samples, and unchanged host-side Icarus grading. The endpoint process remained PID `2348609` throughout the series. Shared endpoint use was permitted and observed, so wall-time and timeout comparisons are descriptive rather than controlled latency measurements.

The bare-model arm used zero examples, no tools, the model generator's Pass@1 defaults, and a 32,768-token output limit. Each Agent arm used:

```text
max input tokens  = 32,768
max output tokens = 32,768
timeout            = 500s
max turns          = 20
max tool calls     = 50
thinking           = enabled
toolset            = rtl
```

`Pi extensions` means the deterministic DCD focused route
`rtl-module → rtl-module-orchestrator`, implemented by producer
`pi-dcd-rtl-module`. It does not include dedicated semantic `eda_lint` or
`eda_simulate` tools. Every one of its 156 trajectories began with the expected
`dcd_dispatch` event. `OpenCode clean` and `Pi clean` did not load the DCD route.

The model-only and Agent arms use different generator implementations by design. Their benchmark inputs and correctness authority are identical, but their generation mechanics and resource accounting are not interchangeable.

### Post-run validity correction

The frozen Agent evaluator in this series contained a candidate-content credential scanner at the Sample Bundle publication seam. It rejected a candidate whenever the credential byte string appeared anywhere in `TopModule.sv`. This violated the producer/evaluator seam: candidate-content safety belongs to the Agent, while the evaluator must publish every structurally admissible candidate byte-for-byte.

The scanner materially affected all three Agent arms:

| Arm | Scanner-rejected candidates | Successful `TopModule.sv` writes observed | Normal HDL declaration collision observed |
| --- | ---: | ---: | ---: |
| OpenCode clean | 13 | 13 | 13 |
| Pi clean | 10 | 10 | 10 |
| Pi extensions | 18 | 18 | 18 |

Those candidates were discarded and replaced with frozen invalid Verilog before hidden grading. Artifact-only policy prohibits reconstructing candidates from trajectory text, so their true correctness is unknowable. The raw scores below remain reproducible evidence of what the frozen evaluator reported, but they are **not admissible evidence for ranking bare model versus Agent capability**. A rerun after removing content-based candidate rejection was therefore required and is recorded in [the 2026-08-04 scanner-free Agent rerun](#2026-08-04-scanner-free-agent-rerun). Diagnostic trajectory/stderr redaction remains separate and cannot alter candidate bytes or submission status.

### Results

| Arm | Pass@1 | Published artifacts | Agent timeouts | Known total tokens | Turns | Tool calls | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bare model | **109/156 = 69.87%** | N/A | N/A | 788,296 | N/A | 0 | 3,785s |
| OpenCode clean | 101/156 = 64.74% | 140/156 | 4 | 3,194,150 | 625 | 511 | 2,293s |
| Pi clean | 99/156 = 63.46% | 139/156 | 9 | 1,954,232 | 534 | 387 | 2,252s |
| Pi extensions | 108/156 = 69.23% | 133/156 | 12 | 3,740,169 | 865 | 770 | 2,495s |

The Agent token totals are lower bounds where timed-out samples did not report usage: OpenCode has two unknown samples, Pi clean seven, and Pi extensions five. Bare-model usage is known for all 156 samples.

Agent submission outcomes were:

| Arm | Published | Invalid | Missing | Passes among published |
| --- | ---: | ---: | ---: | ---: |
| OpenCode clean | 140 | 13 | 3 | 101/140 = 72.14% |
| Pi clean | 139 | 10 | 7 | 99/139 = 71.22% |
| Pi extensions | 133 | 18 | 5 | **108/133 = 81.20%** |

Execution status, artifact publication, and hidden correctness are separate dimensions. In particular, the extension arm published fewer artifacts than either clean Agent arm but had the highest conditional correctness among its published artifacts.

### Paired outcomes

Exact two-sided McNemar tests compare the four arms problem by problem:

| A | B | A only | B only | Both pass | Neither passes | Exact p |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bare model | OpenCode clean | 24 | 16 | 85 | 31 | 0.2682 |
| Bare model | Pi clean | 26 | 16 | 83 | 31 | 0.1641 |
| Bare model | Pi extensions | 18 | 17 | 91 | 30 | 1.0000 |
| OpenCode clean | Pi clean | 11 | 9 | 90 | 46 | 0.8238 |
| OpenCode clean | Pi extensions | 10 | 17 | 91 | 38 | 0.2478 |
| Pi clean | Pi extensions | 11 | 20 | 88 | 37 | 0.1496 |

Pi extensions therefore gained 20 problems and lost 11 relative to Pi clean, for a net gain of nine passes. The paired result does not cross the conventional 0.05 threshold. Bare model and Pi extensions were effectively tied: their scores differ by one, with 18 bare-only and 17 extension-only successes.

### Tool and EDA audit

| Arm | Tool breakdown | Samples with command-line EDA | Samples with functional simulation | EDA/edit/recheck loops |
| --- | --- | ---: | ---: | ---: |
| Bare model | none | 0/156 | 0 | 0 |
| OpenCode clean | `write` 156, `read` 344, `glob` 9, `bash` 2 | 0/156 | 0 | 0 |
| Pi clean | `write` 158, `read` 144, `bash` 79, `edit` 6 | 10/156 | 5 | 5 |
| Pi extensions | `write` 159, `read` 165, `bash` 431, `edit` 15 | **151/156** | **19** | **20** |

The 20 extension-arm EDA/edit/recheck samples produced 17 final hidden-test passes. This is descriptive, not causal: tool use was selected by the Agent, and the clean and extension arms generated different initial RTL. A same-initial-artifact EDA-on/EDA-off experiment is required to isolate the effect of EDA feedback.

No arm invoked a dedicated semantic EDA tool. The extension arm used generic `bash` to invoke command-line Icarus and Verilator. Its audit found 151 availability probes, 197 Icarus compile command matches, 24 Verilator lint command matches, and 36 `vvp` simulation command matches. A single shell command may contribute to more than one category, so these counts are not distinct tool calls.

### Conclusions

- The observed scores were bare model 109/156, Pi extensions 108/156, OpenCode clean 101/156, and Pi clean 99/156. They describe the frozen evaluator output, not an admissible capability ranking, because only the Agent arms suffered content-based candidate rejection.
- The scanner rejected 13 OpenCode, 10 Pi-clean, and 18 Pi-extension candidates after successful artifact writes. Their unknown correctness prevents correction of the scores after the fact.
- The observed Pi-extension versus Pi-clean difference was nine passes, but scanner exposure differed between the arms and the paired result cannot isolate extension efficacy.
- Relative to Pi clean, Pi extensions used approximately 1.91× the known tokens, 1.99× the tool calls, and 1.62× the turns, while recording 12 rather than 9 Agent timeouts. These resource observations remain descriptive.
- Command-line EDA was a normal part of the focused extension behavior, but systematic functional simulation remained uncommon and this experiment does not establish that EDA caused any score difference.
- Each arm also has only one stochastic sample per problem. Even after the evaluator seam is corrected, a stable Harness ordering requires repeated interleaved runs.
- The exact bare-model result remains valid for this specific 32K-output, eight-job profile, but comparisons against the affected Agent arms must be rerun.
- Because this run used model root `/opt/llm/Qwen3.6-27B`, its scores should not be pooled directly with runs made against a different model artifact merely because the served model name is the same.

### Provenance

```text
Experiment root:
/opt/agent/verilogeval-full4-20260803

Bare model:
/opt/agent/verilog-eval/build/nix-eval-1ced7ae6eccf

OpenCode clean:
/opt/agent/verilog-eval-formal/build/a269cf6450c12a06bf7ede97adcb33409e355f15e5673c8008d67446e2cc28e8

Pi clean:
/opt/agent/verilog-eval-formal/build/1a3df8c3ae42a0d6a42459b52dcec8366ed0a801e428749170464d4ad5b142ab

Pi extensions:
/opt/agent/verilog-eval-formal/build/ef9d06b1721f153d06b7744f998402dd1c57267a2951f0203e8e3093404a04ca
```

Source identities:

```text
model-only evaluator: ec4b33025e43be888309f8260f032c4ce8e77f45
Agent evaluator:      8133061876aaf958b019241f601ff5901ada6dc0
DCD integration:      608869856aa0aa431e134aae09b7529c74dc8ff7
```

Frozen evidence and reports:

```text
freeze.json       2b4bea41bc7911f2e9e54906ebf347ca2b183d9072a0e0a6321f6e39dd96056c
run-series.sh     77eecdecf0655b2fb2aed9fd6e87dfdb38696cf8647516de48e15d8253224bc7
problems.txt      301e28ce3c9653a664b4fda2358202eafdaf0f1f8104e08f7a48085a80e6cf06
comparison.json   1d6c6d77ee9d0ce80d80b18843ff770272b561ba1d60196f2fb2e786bbfd2b31
comparison.txt    10534014411705bb68a7399f52c20b0260e86227bf56dd3f03ac0aec1cd29769
tool-usage.json   0d0ec1d38febed73c3effc7fdcf0d056b6bd459e4951e33b498a6546dfee118b
```

All three Agent run roots passed `scripts/validate-agent-run --expected-samples=156`. The series ended with the endpoint identity unchanged, no evaluation containers, no benchmark tmux session, and no temporary sample workspaces.

## 2026-08-04 scanner-free Agent rerun

### Why the evaluator and experiment changed

The 2026-08-03 experiment exposed a boundary error rather than an Agent capability failure. The evaluator passed the API credential into candidate admission and rejected `TopModule.sv` whenever those bytes appeared anywhere in the file. A low-entropy local interface value collided with ordinary HDL declarations such as `localparam`; diagnostic rendering of affected trajectories consequently showed forms such as `[REDACTED]param`.

That behavior coupled three concerns that must remain separate:

1. the Agent owns credential handling and candidate-content safety after retrieval;
2. the evaluator admits a candidate using structural checks only; and
3. diagnostic trajectory and stderr persistence may redact configured values without changing candidate bytes or submission status.

Evaluator commit `07672755cf5b255a33ac103440aa1bc0783bd7b2` removed credential values from candidate inspection. Candidate admission now checks only the expected path, regular-file and symlink properties, non-empty bounded size, and unchanged public-starter identity. A regression test at `commit_sample_bundle()` proves that diagnostic redaction cannot rewrite or reject an otherwise admissible candidate.

A documentation-only correction could not recover the rejected results. The old evaluator had already discarded each original candidate and published frozen invalid Verilog in its place, while the artifact-only contract forbids reconstructing a submission from trajectory text. The three affected Agent arms therefore had to be regenerated and graded again. The bare-model arm did not pass through the Agent Sample Bundle publication seam, so it was not rerun; its exact 2026-08-03 result is retained below as an explicitly inherited comparison row.

### Frozen rerun design

The scanner-free rerun covered all 156 problems once per Agent arm, invoked strictly serially in this order:

```text
OpenCode clean → Pi clean → Pi extensions
```

The rerun retained the earlier Agent configuration:

```text
model               = qwen3.6-coder
model root          = /opt/llm/Qwen3.6-27B
samples             = 1
jobs                = 8
max input tokens    = 32,768
max output tokens   = 32,768
timeout              = 500s
max turns            = 20
max tool calls       = 50
thinking             = enabled
toolset              = rtl
host grader          = unchanged Icarus flow
```

Prompt bytes, sandbox images, tool closures, and the DCD focused route were frozen before execution. The endpoint remained PID `2348609`; shared endpoint traffic was permitted and recorded descriptively. All 156 Pi-extension trajectories began with the expected `dcd_dispatch` event for `rtl-module → rtl-module-orchestrator`.

The series ran from `2026-08-04T02:19:29Z` to final status publication at `2026-08-04T04:17:23Z`, for **1h 57m 54s** total elapsed time. Arm wall time below is measured from that arm's `START` to `DONE` marker. It includes concurrent generation and host grading, and is not the sum of per-sample execution durations. Because the endpoint admitted unrelated shared traffic, wall-time and timeout differences are operational observations rather than controlled latency effects.

### Scanner-free results, including wall time

| Arm | Pass@1 | Published / missing / invalid | Execution exceptions | Known total tokens | Turns | Tool calls | Wall time |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| OpenCode clean | 114/156 = **73.08%** | 151 / 5 / **0** | 6 timeouts | 3,142,614 | 615 | 504 | 2,215s = **36m 55s** |
| Pi clean | 108/156 = **69.23%** | 150 / 6 / **0** | 6 timeouts, 1 error | **1,670,162** | **501** | **355** | **2,143s = 35m 43s** |
| Pi extensions | **122/156 = 78.21%** | 150 / 6 / **0** | 9 timeouts | 3,987,915 | 903 | 808 | 2,713s = **45m 13s** |

Known token totals are lower bounds: usage is unknown for three OpenCode samples, six Pi samples, and six Pi-extension samples. Pi clean was the fastest Agent arm. OpenCode took 72 seconds, or 3.4%, longer than Pi clean. Pi extensions took 570 seconds, or 26.6%, longer than Pi clean and 498 seconds, or 22.5%, longer than OpenCode.

The unaffected bare-model result can be combined with the new Agent results as a scanner-free comparison view, provided that its source and timing caveat remain explicit:

| Arm | Result source | Pass@1 | Wall time |
| --- | --- | ---: | ---: |
| Bare model | inherited unaffected 2026-08-03 arm | 109/156 = 69.87% | 3,785s = 1h 03m 05s |
| OpenCode clean | scanner-free 2026-08-04 rerun | 114/156 = 73.08% | 2,215s = 36m 55s |
| Pi clean | scanner-free 2026-08-04 rerun | 108/156 = 69.23% | **2,143s = 35m 43s** |
| Pi extensions | scanner-free 2026-08-04 rerun | **122/156 = 78.21%** | 2,713s = 45m 13s |

This is not one contemporaneous four-arm series: the bare candidate is inherited, and every problem has only one stochastic candidate per arm. The model-only and Agent generators also have different mechanics and accounting. Score and wall-time comparisons are therefore descriptive; repeated interleaved runs are required for a stable quality or latency ordering.

### Paired outcomes

The first three rows pair the inherited bare result with scanner-free Agent results. They were recomputed by joining the immutable 2026-08-03 and 2026-08-04 `per_problem` records by problem name. The final three rows compare arms generated in the scanner-free series:

| A | B | A only | B only | Both pass | Neither passes | Exact two-sided p |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bare model | OpenCode clean | 13 | 18 | 96 | 29 | 0.4731 |
| Bare model | Pi clean | 19 | 18 | 90 | 29 | 1.0000 |
| Bare model | Pi extensions | 6 | 19 | 103 | 28 | 0.0146 |
| OpenCode clean | Pi clean | 14 | 8 | 100 | 34 | 0.2863 |
| OpenCode clean | Pi extensions | 9 | 17 | 105 | 25 | 0.1686 |
| Pi clean | Pi extensions | 7 | 21 | 101 | 27 | 0.0125 |

Pi extensions gained 21 problems and lost 7 against Pi clean, for a net gain of 14. In this single draw, the exact paired comparison crosses 0.05 for Pi extensions versus Pi clean and for Pi extensions versus the inherited bare result. It does not cross 0.05 for Pi extensions versus OpenCode. These tests quantify the observed problem-level disagreements; they do not remove stochastic, temporal, or shared-load uncertainty.

### Candidate-boundary verification

A post-run audit checked ordinary `localparam` occurrences only after candidates had already been admitted, published, and graded. This audit was evidence collection, not an evaluator admission rule:

| Arm | Invalid submissions | Candidates containing `localparam` | Published unchanged | Hidden-test passes |
| --- | ---: | ---: | ---: | ---: |
| OpenCode clean | **0** | 9 | **9/9** | 7/9 |
| Pi clean | **0** | 11 | **11/11** | 8/11 |
| Pi extensions | **0** | 14 | **14/14** | 12/14 |

All three arms reported zero content-based invalid submissions, and every observed `localparam` candidate crossed the publication seam. This verifies removal of the collision mechanism without making candidate content part of evaluator policy.

### Tool and EDA audit

| Arm | Tool calls | Samples with command-line EDA | Functional simulation samples | EDA/edit/recheck loops | Loop samples passing |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenCode clean | 504 | 0/156 | 0 | 0 | 0 |
| Pi clean | 355 | 7/156 | 5 | 0 | 0 |
| Pi extensions | 808 | **147/156** | **24** | **19** | **13** |

No arm invoked a dedicated semantic EDA tool. Pi extensions used generic `bash` for 210 Icarus compile matches, 36 Verilator lint matches, and 32 `vvp` simulation matches; a shell command can match more than one category. As before, EDA use was Agent-selected and initial RTL differed across arms, so these observations do not establish that EDA feedback caused the score difference.

### Before-and-after record

The earlier scores remain frozen evidence of the flawed evaluator. The following deltas show what the regenerated scanner-free experiment observed; they are not counts of recovered candidates and must not be interpreted causally:

| Arm | Old affected Pass@1 | Scanner-free Pass@1 | Score delta | Invalid submissions | Wall-time change |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenCode clean | 101/156 | 114/156 | +13 | 13 → **0** | 2,293s → 2,215s (-78s) |
| Pi clean | 99/156 | 108/156 | +9 | 10 → **0** | 2,252s → 2,143s (-109s) |
| Pi extensions | 108/156 | 122/156 | +14 | 18 → **0** | 2,495s → 2,713s (+218s) |

The disappearance of `invalid` outcomes and successful publication of all audited `localparam` candidates directly verify the boundary fix. The score and runtime deltas do not measure the scanner's causal effect because the model regenerated every Agent candidate and endpoint load was not controlled.

### Conclusions

- The candidate seam now behaves as specified: structural admission is independent of candidate bytes, while diagnostic redaction remains separate.
- Pi extensions had the highest observed score at 122/156, but also the highest Agent wall time, known token count, turn count, and tool-call count.
- Pi clean was the fastest and least resource-intensive Agent arm, while scoring one pass below the inherited bare result in this draw.
- OpenCode scored six passes above Pi clean with 72 seconds more wall time and substantially more known tokens.
- The scanner-free result supports using these runs as corrected single-draw capability evidence, but not as a stable ranking. Repeated interleaved runs remain necessary.
- Command-line EDA was common in Pi extensions, but a same-initial-artifact EDA-on/EDA-off intervention is still required for causal attribution.

### Provenance

```text
Experiment root:
/opt/agent/verilogeval-agent-clean-rerun-20260804

OpenCode clean:
/opt/agent/verilog-eval-formal/build/4e4820ef8e282ec507d384970292956895bf26367e32630c4563749a3b0cda64

Pi clean:
/opt/agent/verilog-eval-formal/build/24ecda20b74a4b462eb126008d3a770bb287cb719373f5a059dc0f695d57bfab

Pi extensions:
/opt/agent/verilog-eval-formal/build/0c16c95d5ac7d4d0844ddefd43acd5f8fe2e7c746bcb7530c40da2486eba0be5
```

Source identities:

```text
Agent evaluator: 07672755cf5b255a33ac103440aa1bc0783bd7b2
DCD integration: 608869856aa0aa431e134aae09b7529c74dc8ff7
model root:      /opt/llm/Qwen3.6-27B
endpoint PID:    2348609
```

Frozen evidence and reports:

```text
freeze.json       ec456a8ce796f7d8f740fe0395afe7b970c736cf5115ed1cc685da8f62746587
run-series.sh     4adbf655bd90faa3e2436c0133127ecf7754fe8b62823d55be3dd246742b028a
problems.txt      301e28ce3c9653a664b4fda2358202eafdaf0f1f8104e08f7a48085a80e6cf06
schedule.tsv      a5b2fea2d28d8df0222bb2bb53e5cddf21b3b4468dfc3e9e0002778f6e78ee3e
comparison.json   ef2f940f6cf2516b5d93bc1923ef4f068c53727e74231367fdc6404db6c25fe8
comparison.txt    9241402c139aa3dc3a7588274d6ffe80c92d6d7f6ed63a5332ab41257e68725b
tool-usage.json   6459bfee726a76044b131e4afaf3974bc82c1a6b61f11454a202cad00cb2b368
```

All three run roots passed `scripts/validate-agent-run --expected-samples=156`. The rerun ended with the endpoint identity unchanged, zero evaluation containers, no benchmark tmux session, zero temporary sample workspaces, and a clean formal worktree.
