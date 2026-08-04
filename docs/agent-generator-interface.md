# Agent Generator Interface

This document defines the live generator seam and the evidence contract for Agent evaluation.
The implementation is intentionally a deep module: Autoconf and GNU Make know only how to
invoke a selected producer, while `agent_generation/` owns Agent policy, provenance,
isolation, transactions, and reporting.

## Stable outer seam

The configured path is:

```text
scripts/run-agent-evaluation
  → canonical run-config.json + ephemeral runtime-bindings.json
  → configure --with-generator=agent --with-generator-config=<absolute config>
  → GNU Make
  → scripts/sv-invoke-generator
  → scripts/sv-agent-generate
  → agent_generation.sample.generate_agent_sample()
  → fresh Docker container
  → /workspace/TopModule.sv
  → candidate-last Sample Bundle
  → unchanged Icarus grading
  → Python report join
```

GNU Make is the only sample scheduler. It invokes every producer through:

```text
scripts/sv-invoke-generator \
  --program <allowlisted producer> \
  --args-file .generator-args -- \
  --verbose --output <sample.sv> <prompt.txt>
```

Autoconf writes `.generator-args` as mode-0600 NUL-separated UTF-8 records. The invoker
opens it once through a directory descriptor with no-follow semantics, validates type,
owner, mode, link count, size, final NUL, record count, producer-specific shape, and
`execve` capacity, then calls `execv`. It never evaluates a shell command.

The model producer retains its original static argv and implementation. The Agent producer
receives exactly one static record:

```text
--run-config=<absolute content-addressed run-config.json>
```

Its dynamic interface is deliberately narrow:

```text
scripts/sv-agent-generate \
  --run-config=<path> \
  --output <sample.sv> \
  [--verbose] \
  <prompt.txt>
```

No Agent/model semantics are parsed by Autoconf or Make.

## Formal run interface

Use the Nix app, an explicit external tools prefix, and an explicit credential:

```bash
export VERILOG_EVAL_ROOT="$PWD"
export AGENT_EVAL_AGENT_TOOLS=/absolute/path/to/pinned-agent-tools
export OPENAI_API_KEY='...'
export VERILOG_EVAL_JOBS=4

nix run .#agent-eval -- \
  --with-agent=pi \
  --with-model=qwen3.6-coder \
  --with-openai-api-base=http://127.0.0.1:58000/v1 \
  --with-agent-max-input-tokens=16384 \
  --with-max-tokens=16384 \
  --with-agent-thinking=on \
  --with-agent-toolset=standard \
  --run-path-file=/tmp/verilog-agent-run
```

`--with-agent=opencode` selects OpenCode. `--with-agent-toolset=rtl` selects the larger
RTL image. Agent evaluation exposes neither `temperature` nor `top_p`; those remain only
part of the unchanged model-producer interface.

The run path is acknowledged on stdout and, when supplied, durably written to the
mode-0600 `--run-path-file`. Do not infer it from build-directory naming.

## Immutable identity and runtime bindings

`run-config.json` is canonical UTF-8 JSON. Its SHA-256 is both its identity and its
64-hex parent directory. It includes:

- Agent, model, thinking, and toolset;
- exact selected problems and public/hidden input identities;
- timeout, turn, tool-call, input, and output budgets;
- endpoint base URL, exact bounded `/models` response digest, and credential
  environment-variable name, never the value;
- source commit, Docker image/daemon identity, projected tools identities and package
  versions, host toolchain/support identities, and Make concurrency;
- an optional nonce only for `--new-run`.

Machine-local locators are not material identity. They live temporarily in the owned
mode-0600 `runtime-bindings.json`, including separate tools-source and projected-tools
locators. Their executable, support-file, image/daemon, source/input, lock, source-tree,
and projected-tree identities are rechecked against the immutable config before configure.
The bindings are removed and synced after success or failure. The file never enters a container or report.

Ordinary identical invocation resumes the same run. `--new-run` records a nonce and a
durable recovery receipt. Recovery commands are:

```bash
scripts/run-agent-evaluation --build-root <root> --list-recoveries
scripts/run-agent-evaluation --build-root <root> --resume-recovery <digest>
scripts/run-agent-evaluation --build-root <root> --abandon-recovery <digest>
```

Lifecycle and run locks fail rather than wait. Quarantine and rollback operate only after
proving that no run lock, broker socket/listener, Agent container, or matching process is
live.

## Endpoint and credential boundary

The core supports an OpenAI-compatible endpoint, not server-specific control APIs.
Preparation makes one bounded HTTP/1.1 `GET <base>/models` request in a killable helper.
Accepted bases are HTTPS with an optional unreserved prefix ending in `/v1`, or loopback
HTTP with the same grammar. Redirects, proxies, compression, ambiguous framing,
continuation, malformed JSON, and missing exact model IDs fail closed.

The credential value enters preparation through the configured environment name and then
a private run-local Unix broker. The broker validates peer UID, config digest, sample ID,
and environment name. Configure, Make, grading, argv, reports, and persisted runtime
bindings never receive the value. The broker shuts down and its socket is durably removed
before report publication. The Agent owns credential handling after retrieval and owns the
safety of candidate content; the evaluator does not enforce content-security policy on the
candidate.

## Container boundary

Every sample gets a fresh non-root, read-only-root Docker container with:

- writable `/workspace` containing only `TASK.md`, optional `RULES.md`, optional public
  starter `TopModule.sv`, and private Agent config;
- read-only `/agent-tools`, a lock-derived projection containing only the selected Agent
  and its production dependency closure;
- Docker-managed resolver/hostname files;
- no repository, dataset, hidden tests/references, run config, Docker socket, arbitrary
  tools prefix, unrelated environment, secrets other than the selected API key, package
  manager, plugin, skill, session, or network-discovery state.

Only `/workspace/TopModule.sv` is inspected as a submission. Chat text and stdout can never
be recovered as Verilog.

## Sample Bundle transaction

For `<sample>.sv`, the host commits:

```text
<sample>-trajectory.jsonl
<sample>-stderr.log
<sample>-generation.json
<sample>.sv                 # completion marker, committed last
```

The manifest hashes and sizes only candidate, trajectory, and stderr; it never hashes
itself. Diagnostic sidecars are redacted and synced before the candidate is linked into
place. Candidate admission is structural only: a bounded, nonempty regular
`/workspace/TopModule.sv` is published byte-for-byte without content scanning. A missing
or structurally invalid file, or an unchanged public starter, becomes the frozen invalid
Verilog placeholder and still reaches the unchanged grader. Diagnostic redaction cannot
change candidate bytes or submission status. Filesystem, container-control, transaction,
or identity failures publish no accepted bundle.

A valid existing candidate requires all sidecars, canonical manifest bytes, exact hashes,
and the same run-config digest. Regular partial sidecars without a candidate are removed
and synced before regeneration. Suspicious entries quarantine the run.

Execution, submission, and correctness remain orthogonal:

- execution: `completed`, `timeout`, or Agent-process `error`;
- submission: `published`, `missing`, or `invalid`;
- correctness: only the original Icarus summary;
- usage: nullable; unavailable values are never represented as zero.

## Report transaction

After Make succeeds, Python validates the exact expected sample set and joins canonical
`summary.csv` rows with hash-valid Sample Bundles. It writes and syncs
`agent-summary.txt`, then writes and syncs `agent-summary.json` last. The JSON marker binds
the run-config digest, canonical-summary hash, and text hash/length. It does not hash
itself.

A report is complete only when config, bundles, summary, text, and JSON all validate.
Infrastructure failure cannot publish a completed report, but already committed parallel
Sample Bundles remain resumable.

Validate a run without trusting stdout:

```bash
scripts/validate-agent-run --expected-samples=156 "$(cat /tmp/verilog-agent-run)"
```

The validator rejects extra or missing run evidence, unsafe file types/ownership, stale
bindings/socket state, hash mismatches, mixed identities, and report/config disagreement.

## Formal gates

From a clean tracked Linux worktree:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
nix flake check --all-systems --no-build
scripts/regenerate-configure
git diff --exit-code -- configure

tests/integration/model-generator-regression
tests/integration/nix-agent-launcher-smoke
tests/integration/agent-image-isolation-smoke
tests/integration/agent-docker-isolation-smoke
tests/integration/agent-docker-timeout-smoke
tests/integration/agent-docker-budget-smoke
tests/integration/agent-openai-request-smoke
```

The formal 156-sample acceptance run uses `--run-path-file` and validates the resulting
path with `scripts/validate-agent-run`. Selected public and hidden inputs are rehashed
after grading and before report publication; any persistent mutation is infrastructure
failure.
