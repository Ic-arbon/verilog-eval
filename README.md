# VerilogEval Overview

This is an evaluation harness for the VerilogEval problem solving dataset originally described in the paper "[VerilogEval: Evaluating Large Language Models for Verilog Code Generation](https://arxiv.org/abs/2309.07544)," published in 2023. In August 2024, this repository was revised to cover specification-to-RTL tasks in addition to the original code completion task, add in-context learning examples to prompts, and categorize common iverilog failures. Please see the related apaper "[Revisiting VerilogEval: Newer LLMs, In-Context Learning, and Specification-to-RTL Tasks](https://arxiv.org/abs/2408.11053)," published in 2024.

**If you would like to benchmark against the original VerilogEval 1.0 harness, please checkout Git branch "release/1.0.0" which has been kept to preserve this original benchmark. Otherwise, the main branch can be used for the improved harness.**

### VerilogEvalV2 with Reframed Prompts and New Scripts

This repo contains the original VerilogEval dataset with reframed prompts
and new scripts. The original VerilogEval prompts explicitly included the
Verilog module interface, while in this version we specify the module
interface more abstractly. The new scripts manage the dataset as plain
text files (instead of a large JSONL file), include generation and
analysis scripts, and include a Makefile to drive the workflow. The
generation script includes support for easily changing the LLM model,
including/excluding in-context learning rules and in-context learning
examples. The analysis script includes support for categorizing common
iverilog errors and outputing the results in both plain text and CSV
files.

MachineEval is not supported in VerilogEvalV2, only the Human Eval problem statements. Pass@10 is no longer being reported either, instead Pass@1 with number of samples n=1 (temperature=0, top_p=0.01) and n=20 (temperature=0.85, top_p=0.95) for low and high and temperature results, respectively.

### Setup Linux Environment

A pinned Nix flake is provided for the `x86_64-linux` test environment.
Enter it with:

```sh
nix develop
verilog-eval-setup
```

`verilog-eval-setup` creates `.venv` and installs the Python dependencies at
versions compatible with this harness. The development shell automatically
adds that environment to `PATH`, so no manual `uv` command or activation step
is needed. The command is idempotent and can be rerun after dependency changes.

The shell also contains the Verilog simulators, Python 3.11, and GNU
command-line tools used by the evaluation harness. The lock file pins nixpkgs
for a reproducible base environment.

Run the complete evaluation against the local `qwen3.6-coder` vLLM directly
from the repository root:

```sh
nix run
```

The default app uses `http://127.0.0.1:58000/v1` and automatically reads
`VLLM_API_KEY` from `/opt/llm/api-key.env` when that file exists; otherwise it
uses a harmless placeholder required by the OpenAI client. It checks the vLLM
health endpoint before starting, so no environment variables are normally
needed. The explicit entry point is `nix run .#vllm`.

The runner installs the pinned Python dependencies when needed, defaults to
the Pass@1 configuration (`samples=1`, `max_tokens=8192`, `temperature=0.6`,
`top_p=0.95`), and runs Make with one parallel job per available CPU core. The
larger response limit leaves room for reasoning models to finish their answer
before emitting the final Verilog code.
Configure options can be appended after `--`, for example:

```sh
nix run -- --with-task=code-complete-iccad2023
```

For another OpenAI-compatible or hosted model, use the generic entry point and
provide its credentials:

```sh
OPENAI_API_KEY="..." nix run .#eval -- --with-model=gpt-4o
```

Set `VERILOG_EVAL_JOBS` to override the detected parallelism. Each distinct
configuration is kept in its own `build/nix-eval-*` directory so interrupted
runs can resume without mixing results from different configurations.

The `qwen3.6-coder` model is included in the OpenAI-compatible whitelist. To
use a remote vLLM or the optional LiteLLM gateway instead, override
`OPENAI_API_BASE` and `OPENAI_API_KEY`; the defaults remain suitable when the
evaluation runs on the vLLM host itself.

### External Agent evaluation

See the [Agent testing guide](docs/agent-evaluation.md) and normative
[generator interface](docs/agent-generator-interface.md).

Pi and OpenCode use the same producer-neutral generator seam as the model path.
GNU Make remains the only sample scheduler, and the unchanged
Icarus/`sv-iv-analyze` path remains the correctness authority. The only formal
submission is the host-inspected `/workspace/TopModule.sv`; chat and stdout are
never recovered as code.

Start with one problem and an explicit pinned tools prefix:

```sh
printf 'Prob001_zero\n' >/tmp/agent-smoke.txt
run_path_file="$(mktemp)"
chmod 0600 "$run_path_file"

export AGENT_EVAL_AGENT_TOOLS=/absolute/path/to/agent-tools
export OPENAI_API_KEY='...'
export VERILOG_EVAL_JOBS=1

nix run .#agent-eval -- \
  --with-agent=opencode \
  --with-problems=/tmp/agent-smoke.txt \
  --with-agent-max-input-tokens=16384 \
  --with-max-tokens=16384 \
  --with-agent-thinking=on \
  --run-path-file="$run_path_file"

scripts/validate-agent-run --expected-samples=1 "$(cat "$run_path_file")"
```

Change the Agent to `pi` as needed. The formal app never installs or downloads
Agent tools. It projects only the selected lock-derived production closure into
a read-only `/agent-tools` mount. Each sample gets a fresh non-root, read-only-root
container containing only its public workspace; repository, dataset, hidden grader
files, previous results, Docker socket, full run config, and unrelated environment
are absent.

Material config is canonical JSON addressed by its SHA-256. Machine locators are
ephemeral, the API key is handed to samples through a private run-local broker, and
both are removed before report publication. Agent defaults are one sample,
`qwen3.6-coder`, 16384 input tokens, 16384 output tokens, 300 seconds, 20 turns,
50 completed tool calls, thinking on, the standard toolset, and four Make jobs.
Agent evaluation has no sampling controls.

Each Sample Bundle commits scrubbed trajectory, stderr, canonical manifest, and
candidate last. The final Python report joins these hash-valid bundles with canonical
Icarus rows and commits text first and JSON last. Execution, submission, correctness,
and nullable usage remain separate.

Run the adversarial Linux gates before a larger evaluation:

```sh
tests/integration/model-generator-regression
tests/integration/nix-agent-launcher-smoke
tests/integration/agent-image-isolation-smoke
tests/integration/agent-docker-isolation-smoke
tests/integration/agent-docker-timeout-smoke
tests/integration/agent-docker-budget-smoke
tests/integration/agent-openai-request-smoke
```

To verify the core tools:

```sh
iverilog -V
verilator --version
python3 --version
```

For a non-Nix setup, you will need to install iverilog, verilator,
and python3 along with several Python packages. These are the versions
which were used for this project:

 - iverilog (v12)
 - python3 (v3.11.0)

**Please note that iverilog v13 (development release) is not supported.**

To install Python 3.11:
```
$ conda create -n codex python=3.11
$ conda activate codex
```

Install [ICARUS Verilog](https://github.com/steveicarus/iverilog):
```
$ git clone https://github.com/steveicarus/iverilog.git && cd iverilog \
        && git checkout v12-branch \
        && sh ./autoconf.sh && ./configure && make -j4\
        && make install
```

You will also need the following Python packages:

```
 % pip install langchain langchain-openai langchain-nvidia-ai-endpoints
```

We plan to provide a Dockerfile and backwards compatibility mode with a prebuilt jsonl soon.

### Usage 

The evalution harness is run using make and various evaluation parameters can be set as below:

```
mkdir -p build/
../configure  --with-task=$task --with-model=$model --with-examples=$shots --with-samples=$samples --with-temperature=$temperature --with-top-p=$top_p
make
```

Evaluation can be sped up by providing the `-j` flag to make, such as `-j4` to run 4 worker processes.

Available tasks are `code-complete-iccad2023` and `spec-to-rtl` with each referencing their corresponding `dataset_$task` directory containig the problems. Problem themselves are identical between the two datasets and only the task format changes.

Valid models are listed at the top of `scripts/sv-generate`. The number of in-context learning examples can be between 0-4, and given with `--with-examples`. Samples to collect per problem are given by `--with-samples`. Finally, model temperature and top_p can be set to --with-temperature and --with-top-p, respectively.

These parameters can be easily swept with a shell script, to create separate build directories for each evaluation harness configuration target. 

## Citation

For this VerilogEval v2, please cite the following paper:

```
@misc{pinckney2024revisitingverilogevalnewerllms,
      title={Revisiting VerilogEval: Newer LLMs, In-Context Learning, and Specification-to-RTL Tasks}, 
      author={Nathaniel Pinckney and Christopher Batten and Mingjie Liu and Haoxing Ren and Brucek Khailany},
      year={2024},
      eprint={2408.11053},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2408.11053}, 
}
```

For the original VerilogEval v1, please use:

```
@inproceedings{liu2023verilogeval,
  title={{VerilogEval:} Evaluating Large Language Models for Verilog Code Generation},
  author={Liu, Mingjie and Pinckney, Nathaniel and Khailany, Brucek and Ren, Haoxing},
  booktitle={2023 IEEE/ACM International Conference on Computer-Aided Design (ICCAD)}, 
  year={2023}
}
```
