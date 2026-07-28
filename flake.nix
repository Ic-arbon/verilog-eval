# EDA open-source toolchain devShell — CANONICAL, pinned to nixpkgs 25.11.
#
# This file is authoritative and pre-verified: every nixpkgs attr below was
# evaluated against nixpkgs 25.11 (nixos-25.11, commit b6018f87, 2026-06-30)
# on x86_64-linux, and the full devShell (including the librelane input) has a
# clean derivation instantiation. Do NOT let an offline LLM re-derive package
# names — copy this file verbatim to the project root as `flake.nix`, then run
# `nix flake lock` and `nix develop`.
#
# Target: x86_64-linux (the EDA flows run on Linux).
#
# Coverage:
#   • Core open-source tools — from nixpkgs 25.11 (buildInputs, first group).
#   • openlane + standalone OpenSTA — from the official librelane flake
#     (github:librelane/librelane; OpenLane2 was renamed "librelane").
#   • Still NOT packaged (source build required) — gem5, bambu; see the note
#     at the bottom.
{
  description = "EDA open-source toolchain devShell — nixpkgs 25.11 + librelane (x86_64-linux)";

  # librelane pulls in a custom nix-eda toolchain (custom iverilog,
  # yosys-with-plugins/yosys-slang) that is NOT in cache.nixos.org. Without the
  # fossi-foundation binary cache these build from source and FAIL on
  # constrained hosts (yosys-slang source-fetch timeouts, iverilog check
  # failures). This substituter serves the prebuilt x86_64-linux closure.
  # Verified: the yosys-slang / yosys-with-plugins / iverilog outputs are 200.
  # Requires `nix develop --accept-flake-config`, OR add these two lines to
  # ~/.config/nix/nix.conf (then no flag is needed):
  #   extra-substituters = https://nix-cache.fossi-foundation.org
  #   extra-trusted-public-keys = nix-cache.fossi-foundation.org:3+K59iFwXqKsL7BNu6Guy0v+uTlwsxYQxjspXzqLYQs=
  nixConfig = {
    extra-substituters = [ "https://nix-cache.fossi-foundation.org" ];
    extra-trusted-public-keys = [ "nix-cache.fossi-foundation.org:3+K59iFwXqKsL7BNu6Guy0v+uTlwsxYQxjspXzqLYQs=" ];
  };

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    # librelane keeps its OWN nixpkgs pin on purpose — do NOT add
    # `inputs.nixpkgs.follows = "nixpkgs"`, it needs its vetted closure.
    librelane.url = "github:librelane/librelane";
  };

  outputs = { self, nixpkgs, librelane }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lr = librelane.packages.${system};   # provides: librelane, opensta, openroad, openroad-abc
      # cocotb is a Python package — provide it through a Python that has it
      py = pkgs.python3.withPackages (p: [ p.cocotb ]);
    in {
      devShells.${system}.default = pkgs.mkShell {
        buildInputs = (with pkgs; [
          # --- RTL compile / simulation ---
          # verilator 由 librelane 自带（5.044），不重复引入 nixpkgs 的 5.040
          iverilog           # iverilog       — Icarus Verilog compiler + vvp
          yosys              # yosys          — synthesis + formal front-end
          gtkwave            # gtkwave        — waveform viewer (.vcd/.fst)
          abc-verifier       # abc            — Berkeley ABC (binary: `abc`)
          sby                # sby            — SymbiYosys formal driver

          # --- SystemVerilog parsers / converters ---
          sv-lang              # slang          — SV compiler / linter (注意：不是 pkgs.slang，那是 S-Lang 脚本库)
          surelog            # surelog        — SV 2017 preprocessor/parser
          haskellPackages.sv2v  # sv2v        — SV-to-Verilog (top-level attr absent; lives under haskellPackages)

          # --- physical design ---
          klayout            # klayout        — GDS viewer / DRC
          # openroad           # librelane 自带 openroad 2026-02-17（更新），nixpkgs 的是 2025-03-01（旧）
          nextpnr            # nextpnr        — FPGA place-and-route
          openfpgaloader     # openFPGALoader — FPGA bitstream programming
          openocd            # openocd        — on-chip debugger

          # --- analog / schematic ---
          xschem             # xschem         — schematic capture

          # --- Python + toolchain ---
          py                 # python3 + cocotb (co-simulation)
          llvm               # llvm-config    — LLVM toolchain
          gcc                # gcc            — GNU compiler
          uv                 # uv             — fast Python package manager
        ]) ++ [
          # --- ASIC flow + STA, from the librelane flake ---
          lr.librelane       # openlane        — OpenLane2/LibreLane RTL-to-GDS flow (command: `librelane`)
          lr.opensta         # sta             — standalone OpenSTA static timing analyzer
        ];

        shellHook = ''
          echo "=== EDA devShell (nixpkgs 25.11 + librelane) ready ==="
          # tool-specific root vars (replace Environment-Modules setenv lines)
          # Unset VERILATOR_ROOT: librelane bundles its own verilator (5.044)
          # which conflicts with the nixpkgs verilator (5.040) setup-hook export.
          # Verilator's Perl wrapper auto-detects the correct root when unset.
          unset VERILATOR_ROOT
          export YOSYS_DATDIR=${pkgs.yosys}/share/yosys
          export LLVM_DIR=${pkgs.llvm.dev}
        '';
      };
    };
}

# ─── NOT packaged — source build required (verified absent in nixpkgs 25.11) ──
# Do not fabricate derivations inline. If a flow needs these, build from source
# in a dedicated derivation / flake input:
#
#   • gem5   — micro-architectural simulator (SCons build; heavy, ~30+ min)
#              upstream: https://github.com/gem5/gem5
#   • bambu  — PandA-bambu HLS (autotools, Linux only; LLVM-version sensitive)
#              upstream: https://github.com/ferrandi/PandA-bambu
#              (a buildFHSEnv wrapper around the vendor binary is often easier
#               than a pure-nix derivation)
