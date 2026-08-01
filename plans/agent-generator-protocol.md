# Generator Protocol Construction Record

Status: **superseded by the completed generator-seam cutover**.

The original construction plan described the first Make-driven Agent producer. Its broad
configured flags, numbered profiles/schemas, generator-specific Make conditionals, and
direct report recipe were intentionally removed by
[`deepen-generator-seam.md`](deepen-generator-seam.md).

The live normative interface is
[`../docs/agent-generator-interface.md`](../docs/agent-generator-interface.md).

## Historical intent retained

The first plan established invariants that remain authoritative:

1. GNU Make is the only per-sample scheduler.
2. Pi and OpenCode are producers, not graders.
3. The original Icarus/hidden-test path is the correctness authority.
4. Each sample runs in a fresh non-root Docker container with no repository, dataset,
   hidden reference/test, previous result, Docker socket, or unrelated environment.
5. `/workspace/TopModule.sv` is the only formal submission source.
6. Execution, submission, correctness, and nullable usage are independent evidence axes.
7. Agent tools must be explicit and reproducible; formal execution never installs them.
8. Infrastructure failure must not be converted into an ordinary Agent error row.

## Why the interface was replaced

The initial implementation leaked Agent/model semantics across Autoconf, Make, shell/Nix,
drivers, sidecar writers, and reports. A change to one Agent option required synchronized
edits across shallow modules, and configured shell text could not preserve arbitrary argv
safely. Runtime locators and material identity were also mixed, reports were produced from
Make, and cumulative transport events could dominate storage.

The accepted redesign selected:

```text
Autoconf-generated NUL-separated static argv
  → producer-neutral no-shell execv adapter
  → unchanged model producer OR narrow Agent producer
```

Deep Python modules now own immutable configuration, endpoint/credential preparation,
tools projection, provenance, run lifecycle, sample/report transactions, and report joins.
Autoconf and Make know only the stable producer seam.

## Completion map

| Concern | Live owner |
|---|---|
| canonical run identity/publication | `agent_generation/run_config.py` |
| machine-local ephemeral locators | `agent_generation/runtime_bindings.py` |
| run/recovery locks and quarantine | `agent_generation/lifecycle.py` |
| endpoint preflight | `agent_generation/endpoint.py` |
| private credential handoff | `agent_generation/credentials.py` |
| lock-derived tools projection | `agent_generation/tools.py` |
| run composition | `agent_generation/run.py` |
| one Make-selected sample | `agent_generation/sample.py` |
| container control | `agent_generation/docker.py` |
| stable result predicate | `agent_generation/result_contract.py` |
| candidate-last Sample Bundle | `agent_generation/sample_result.py` |
| Icarus/manifest report join | `agent_generation/report.py` |
| generic configured invocation | `scripts/sv-invoke-generator` |
| narrow Agent producer | `scripts/sv-agent-generate` |
| formal runner | `scripts/run-agent-evaluation` / Nix `agent-eval` app |
| completed-run validation | `scripts/validate-agent-run` |

## Acceptance evidence

The cutover is accepted only from a clean Linux worktree after:

- full Python unit and integration-contract suites;
- reproducible Autoconf generation;
- Nix evaluation, launcher closure/exec-graph checks, and image canary inspection;
- unchanged model-producer and Icarus-byte regression;
- Docker isolation, timeout, and budget smokes;
- pinned Pi/OpenCode request/retry/tool-continuation smokes proving no Agent sampling fields;
- one 156-sample run per Agent at concurrency 16;
- exact validation of every formal run via `scripts/validate-agent-run`.

Any future change should update the live interface document and its executable contracts,
not revive this superseded construction sequence or add a compatibility runtime.
