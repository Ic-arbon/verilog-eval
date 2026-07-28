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

Run the complete evaluation directly from the repository root:

```sh
export OPENAI_API_KEY="..."
nix run
```

The runner installs the pinned Python dependencies when needed, defaults to
the low-temperature Pass@1 configuration (`samples=1`, `temperature=0`,
`top_p=0.01`), and runs Make with one parallel job per available CPU core.
Configure options can be appended after `--`, for example:

```sh
nix run .#eval -- --with-task=code-complete-iccad2023 --with-model=gpt-4o
```

Set `VERILOG_EVAL_JOBS` to override the detected parallelism. Each distinct
configuration is kept in its own `build/nix-eval-*` directory so interrupted
runs can resume without mixing results from different configurations.

The `qwen3.6-coder` model served by an OpenAI-compatible vLLM endpoint is also
whitelisted. Point the client at the existing server without changing its
configuration:

```sh
export OPENAI_API_BASE="http://<vllm-host>:58000/v1"
export OPENAI_API_KEY="local" # use the real key when the endpoint requires one
nix run .#eval -- --with-model=qwen3.6-coder
```

For the optional LiteLLM gateway, use port `4000` and a LiteLLM virtual key
instead.

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
