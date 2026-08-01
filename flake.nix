{
  description = "VerilogEval test environment (x86_64-linux)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      runtimeLibraryPath = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];

      nodeRuntime = pkgs.runCommand "verilog-agent-node-runtime" {
        nativeBuildInputs = [ pkgs.removeReferencesTo ];
      } ''
        mkdir -p "$out/bin"
        cp ${pkgs.nodejs_22}/bin/node "$out/bin/node"
        chmod 0555 "$out/bin/node"
        remove-references-to -t ${pkgs.nodejs_22} "$out/bin/node"
      '';

      agentSandboxPackages = [
        nodeRuntime
      ] ++ (with pkgs; [
        bash
        iverilog
        python311
        coreutils
        gnumake
        gitMinimal
        gnugrep
        gnused
        findutils
        util-linux
        which
        stdenv.cc.cc.lib
      ]);
      minimalRtlSandboxPackages = agentSandboxPackages ++ (with pkgs; [
        verilator
        yosys
        abc-verifier
        sby
        sv-lang
        surelog
        haskellPackages.sv2v
      ]);
      agentSandboxPath = pkgs.lib.makeBinPath agentSandboxPackages;
      minimalRtlSandboxPath = pkgs.lib.makeBinPath minimalRtlSandboxPackages;
      agentSandboxImageName = "verilog-eval-agent-sandbox";
      mkAgentSandboxImage = tag: packages: sandboxPath:
        pkgs.dockerTools.buildLayeredImage {
          name = agentSandboxImageName;
          inherit tag;
          contents = packages;
          extraCommands = ''
            mkdir -p bin lib lib64 usr/bin home/agent workspace tmp
            ln -sfn ${pkgs.bash}/bin/bash bin/bash
            ln -sfn ${pkgs.bash}/bin/bash bin/sh
            ln -sfn ${pkgs.coreutils}/bin/env usr/bin/env
            ln -sfn ${pkgs.glibc}/lib/ld-linux-x86-64.so.2 lib64/ld-linux-x86-64.so.2
            ln -sfn ${pkgs.glibc}/lib/libc.so.6 lib/libc.so.6
            ln -sfn ${pkgs.glibc}/lib/libpthread.so.0 lib/libpthread.so.0
            ln -sfn ${pkgs.glibc}/lib/libdl.so.2 lib/libdl.so.2
            ln -sfn ${pkgs.glibc}/lib/libm.so.6 lib/libm.so.6
            chmod 1777 home/agent workspace tmp
          '';
          config = {
            WorkingDir = "/workspace";
            Env = [
              "PATH=${sandboxPath}"
              "HOME=/home/agent"
              "SHELL=/bin/bash"
            ];
          };
        };
      agentSandboxImageTag = "standard";
      minimalRtlSandboxImageTag = "rtl";
      agentSandboxImage = mkAgentSandboxImage
        agentSandboxImageTag agentSandboxPackages agentSandboxPath;
      minimalRtlSandboxImage = mkAgentSandboxImage
        minimalRtlSandboxImageTag minimalRtlSandboxPackages minimalRtlSandboxPath;

      pythonRequirements = pkgs.writeText "verilog-eval-requirements.txt" ''
        langchain==0.2.17
        langchain-community==0.2.19
        langchain-openai==0.1.25
        langchain-nvidia-ai-endpoints==0.2.2
        pandas==2.2.3
      '';

      agentToolsPackageJson = pkgs.writeText "agent-eval-package.json" (builtins.toJSON {
        private = true;
        dependencies = {
          "@earendil-works/pi-coding-agent" = "0.82.1";
          "opencode-ai" = "1.18.7";
        };
      });

      setupAgentTools = pkgs.writeShellApplication {
        name = "verilog-agent-tools-setup";
        runtimeInputs = [ pkgs.coreutils pkgs.gitMinimal pkgs.nodejs_22 ];
        text = ''
          root="''${VERILOG_EVAL_ROOT:-}"
          if [[ -z "$root" ]]; then
            root="$(git rev-parse --show-toplevel)"
          fi

          cache_root="''${VERILOG_EVAL_CACHE_ROOT:-$root/.cache}"
          mkdir -p "$cache_root/npm"
          export XDG_CACHE_HOME="$cache_root"
          export npm_config_cache="$cache_root/npm"

          tools="$root/.agent-tools"
          marker="pi=0.82.1 opencode=1.18.7"
          if [[ ! -x "$tools/node_modules/.bin/pi" \
             || ! -x "$tools/node_modules/.bin/opencode" \
             || "$(cat "$tools/.versions" 2>/dev/null || true)" != "$marker" ]]; then
            mkdir -p "$tools"
            cp ${agentToolsPackageJson} "$tools/package.json"
            npm install --prefix "$tools" --no-audit --no-fund
            printf '%s\n' "$marker" > "$tools/.versions"
          fi

          echo "External agents ready: $marker"
        '';
      };

      setupPython = pkgs.writeShellApplication {
        name = "verilog-eval-setup";
        runtimeInputs = [ pkgs.gitMinimal pkgs.uv ];
        text = ''
          export LD_LIBRARY_PATH="${runtimeLibraryPath}:''${LD_LIBRARY_PATH:-}"

          root="''${VERILOG_EVAL_ROOT:-}"
          if [[ -z "$root" ]]; then
            root="$(git rev-parse --show-toplevel)"
          fi

          venv="$root/.venv"
          if [[ ! -x "$venv/bin/python" ]]; then
            uv venv --python ${pkgs.python311}/bin/python3 "$venv"
          fi

          uv pip install \
            --python "$venv/bin/python" \
            --requirements ${pythonRequirements}

          "$venv/bin/python" -c \
            "import langchain, langchain_community, langchain_openai, langchain_nvidia_ai_endpoints, pandas"
          echo "Python dependencies are ready in $venv"
        '';
      };

      runEvaluation = pkgs.writeShellApplication {
        name = "verilog-eval-run";
        runtimeInputs = with pkgs; [
          setupPython
          gitMinimal
          iverilog
          python311
          gnumake
          bash
          coreutils
          util-linux
          gnugrep
          gnused
        ];
        text = ''
          export LD_LIBRARY_PATH="${runtimeLibraryPath}:''${LD_LIBRARY_PATH:-}"

          root="''${VERILOG_EVAL_ROOT:-}"
          if [[ -z "$root" ]]; then
            root="$(git rev-parse --show-toplevel)"
          fi
          export VERILOG_EVAL_ROOT="$root"
          source_revision="$(git -C "$root" rev-parse HEAD)"
          source_diff_sha256="$(
            git -C "$root" diff --no-ext-diff --binary HEAD \
              | sha256sum \
              | cut -d' ' -f1
          )"
          export VERILOG_EVAL_SOURCE_REVISION="$source_revision"
          export VERILOG_EVAL_SOURCE_DIFF_SHA256="$source_diff_sha256"

          verilog-eval-setup
          export PATH="$root/.venv/bin:$PATH"

          jobs="''${VERILOG_EVAL_JOBS:-$(nproc)}"
          if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
            echo "VERILOG_EVAL_JOBS must be a positive integer" >&2
            exit 2
          fi

          # Default to the benchmark Pass@1 configuration.
          # Later user arguments override these defaults.
          configure_args=(
            --with-samples=1
            --with-max-tokens=8192
            --with-temperature=0.6
            --with-top-p=0.95
            "$@"
          )
          config_key="$(
            printf '%s\0' \
              "$source_revision" \
              "$source_diff_sha256" \
              "''${OPENAI_API_BASE:-unset}" \
              "''${configure_args[@]}" \
              | sha256sum \
              | cut -c1-12
          )"
          build_root="''${VERILOG_EVAL_BUILD_ROOT:-$root/build}"
          build_dir="$build_root/nix-eval-$config_key"
          mkdir -p "$build_dir"

          echo "Configuring evaluation in $build_dir"
          echo "Running make with $jobs parallel jobs"
          cd "$build_dir"
          "$root/configure" "''${configure_args[@]}"
          exec make --jobs="$jobs" SHELL=${pkgs.bash}/bin/bash
        '';
      };

      agentRuntimePath = pkgs.lib.makeBinPath (with pkgs; [
        python311
        docker_29
        iverilog
        gnumake
        gitMinimal
        coreutils
        util-linux
        gnugrep
        gnused
        bash
      ]);

      runAgentEvaluation = pkgs.writeTextFile {
        name = "verilog-agent-eval";
        destination = "/bin/verilog-agent-eval";
        executable = true;
        text = ''#!${pkgs.python311}/bin/python3 -I
import os
import subprocess
import sys

python = "${pkgs.python311}/bin/python3"
git = "${pkgs.gitMinimal}/bin/git"
root = os.environ.get("VERILOG_EVAL_ROOT")
if not root:
    completed = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "${pkgs.gitMinimal}/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    root = completed.stdout.strip()
root = os.path.realpath(root)

environment = dict(os.environ)
for name in tuple(environment):
    upper = name.upper()
    if (
        upper.startswith(("GIT_", "PYTHON", "DYLD_", "NPM_"))
        or upper in {"BASH_ENV", "ENV", "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH"}
        or upper.endswith("_PROXY")
    ):
        environment.pop(name, None)
environment.update({
    "PATH": "${agentRuntimePath}",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "VERILOG_EVAL_ROOT": root,
    "VERILOG_EVAL_CACHE_ROOT": environment.get("VERILOG_EVAL_CACHE_ROOT", os.path.join(root, ".cache")),
    "AGENT_EVAL_DOCKER": "${pkgs.docker_29}/bin/docker",
    "AGENT_EVAL_DOCKER_IMAGE_STANDARD": "${agentSandboxImageName}:${agentSandboxImageTag}",
    "AGENT_EVAL_DOCKER_ARCHIVE_STANDARD": "${agentSandboxImage}",
    "AGENT_EVAL_DOCKER_IMAGE_RTL": "${agentSandboxImageName}:${minimalRtlSandboxImageTag}",
    "AGENT_EVAL_DOCKER_ARCHIVE_RTL": "${minimalRtlSandboxImage}",
})
os.execve(
    python,
    [python, "-I", "-B", os.path.join(root, "scripts/run-agent-evaluation"), *sys.argv[1:]],
    environment,
)
'';
      };

      runVllmEvaluation = pkgs.writeShellApplication {
        name = "verilog-eval-vllm";
        runtimeInputs = with pkgs; [ runEvaluation curl gnugrep coreutils ];
        text = ''
          export OPENAI_API_BASE="''${OPENAI_API_BASE:-http://127.0.0.1:58000/v1}"

          if [[ -z "''${OPENAI_API_KEY:-}" ]]; then
            key_file="''${VERILOG_EVAL_VLLM_KEY_FILE:-/opt/llm/api-key.env}"
            if [[ -r "$key_file" ]]; then
              key_line="$(grep -m1 '^VLLM_API_KEY=' "$key_file" || true)"
              export OPENAI_API_KEY="''${key_line#VLLM_API_KEY=}"
            fi
            export OPENAI_API_KEY="''${OPENAI_API_KEY:-local}"
          fi

          health_url="''${OPENAI_API_BASE%/v1}/health"
          if ! curl --fail --silent --show-error "$health_url" >/dev/null; then
            echo "vLLM is not healthy at $health_url" >&2
            exit 1
          fi

          echo "Using qwen3.6-coder at $OPENAI_API_BASE"
          exec verilog-eval-run --with-model=qwen3.6-coder "$@"
        '';
      };
    in
    {
      packages.${system} = {
        setup = setupPython;
        eval = runEvaluation;
        vllm = runVllmEvaluation;
        agent-eval = runAgentEvaluation;
        agent-tools-setup = setupAgentTools;
        agent-sandbox-image = agentSandboxImage;
        agent-rtl-sandbox-image = minimalRtlSandboxImage;
      };

      apps.${system} = {
        default = {
          type = "app";
          program = "${runVllmEvaluation}/bin/verilog-eval-vllm";
          meta.description = "Run VerilogEval against the local qwen3.6-coder vLLM";
        };
        vllm = {
          type = "app";
          program = "${runVllmEvaluation}/bin/verilog-eval-vllm";
          meta.description = "Run VerilogEval against the local qwen3.6-coder vLLM";
        };
        eval = {
          type = "app";
          program = "${runEvaluation}/bin/verilog-eval-run";
          meta.description = "Run VerilogEval with all available CPU cores";
        };
        agent-eval = {
          type = "app";
          program = "${runAgentEvaluation}/bin/verilog-agent-eval";
          meta.description = "Evaluate Pi and OpenCode in isolated workspaces";
        };
        setup = {
          type = "app";
          program = "${setupPython}/bin/verilog-eval-setup";
          meta.description = "Install VerilogEval Python dependencies into .venv";
        };
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          # Verilog compilation and simulation
          iverilog
          verilator

          # Evaluation harness
          python311
          setupPython
          autoconf
          gnumake
          bash
          coreutils  # seq, timeout, expr
          util-linux # column
        ];

        # Makefile.in uses Bash-specific syntax such as [[ ... ]] and PIPESTATUS.
        MAKEFLAGS = "SHELL=${pkgs.bash}/bin/bash";

        shellHook = ''
          export LD_LIBRARY_PATH="${runtimeLibraryPath}:''${LD_LIBRARY_PATH:-}"
          export VERILOG_EVAL_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
          export PATH="$VERILOG_EVAL_ROOT/.venv/bin:$PATH"

          echo "VerilogEval test environment ready"
          echo "  iverilog : $(iverilog -V 2>&1 | head -n1)"
          echo "  verilator: $(verilator --version)"
          echo "  python   : $(python3 --version)"
          if [[ ! -x "$VERILOG_EVAL_ROOT/.venv/bin/python" ]]; then
            echo "  dependencies: run verilog-eval-setup once"
          fi
        '';
      };
    };
}
