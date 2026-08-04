# VerilogEval Generator Seam Context

This repository has one benchmark scheduler: GNU Make.

## Domain terms

- **Generator Seam** — the producer-neutral invocation boundary owned by Autoconf and Make. Make supplies a selected program, opaque static arguments, an output path, and a public prompt; it does not interpret producer semantics.
- **Run Configuration** — canonical immutable JSON containing every material Agent, benchmark-input, limit, endpoint-name, toolchain, sandbox, and concurrency identity. Exact bytes are content-addressed. It never contains machine-local locator paths or credential values.
- **Runtime Binding** — ephemeral nonsecret mapping from material identities to machine-local paths. It is validated against the Run Configuration and is neither mounted into a container nor accepted as evidence.
- **Input Manifest** — the ordered content identities for every selected public and hidden benchmark input. Python may hash these inputs but only Make schedules their generation and grading.
- **Tools Projection** — a private minimal read-only tree copied from the explicit Agent-tools prefix according to its lock file. Only this projection is mounted at `/agent-tools`.
- **Sample Bundle** — byte-preserved candidate, normalized trajectory, scrubbed stderr, and manifest. Candidate publication is its linearization point after durable sidecars.
- **Canonical Candidate** — the only Make-visible sample output. A bounded, nonempty regular `/workspace/TopModule.sv` is published byte-for-byte without content scanning; only a missing or structurally invalid file, or an unchanged public starter, becomes the frozen invalid Verilog placeholder. Chat and stdout are never candidate sources.
- **Report Pair** — human-readable text committed and synced before a canonical JSON completion marker that binds the Run Configuration, canonical Icarus summary, and text bytes.
- **Recovery Receipt** — durable acknowledgement state for a nonce-bearing new run. It closes the crash gap between immutable config publication and delivery of the run path.
- **Infrastructure Failure** — a control-plane, identity, filesystem, broker, container, transaction, or evidence failure. It publishes neither an accepted Sample Bundle for the affected sample nor a completed report for the run.

## Authorities and boundaries

GNU Make remains the only per-sample scheduler. Hidden tests, Icarus, and `scripts/sv-iv-analyze` remain the correctness authority. The Agent owns the safety and semantics of candidate content; the evaluator performs structural file admission only and never changes submission status based on candidate bytes. Diagnostic redaction is separate from candidate admission and cannot alter the submitted artifact. Execution, submission, and correctness are independent. Unknown usage remains unavailable rather than becoming zero. Infrastructure failures do not become benchmark sample outcomes.
