{
  description = "VerilogEval test environment (x86_64-linux)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      runtimeLibraryPath = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.glibc ];

      agentSandboxPackages = with pkgs; [
        nodejs_22
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
      ];
      agentSandboxPath = pkgs.lib.makeBinPath agentSandboxPackages;
      agentStoreRoots = pkgs.lib.concatStringsSep " " (map toString agentSandboxPackages);
      agentSandboxImageName = "verilog-eval-agent-sandbox";
      agentSandboxImageTag = "v1";
      agentSandboxImage = pkgs.dockerTools.buildLayeredImage {
        name = agentSandboxImageName;
        tag = agentSandboxImageTag;
        contents = agentSandboxPackages;
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
            "PATH=${agentSandboxPath}"
            "HOME=/home/agent"
            "SHELL=/bin/bash"
          ];
        };
      };

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

          verilog-eval-setup
          export PATH="$root/.venv/bin:$PATH"

          jobs="''${VERILOG_EVAL_JOBS:-$(nproc)}"
          if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
            echo "VERILOG_EVAL_JOBS must be a positive integer" >&2
            exit 2
          fi

          # Default to the benchmark's low-temperature Pass@1 configuration.
          # Later user arguments override these defaults.
          configure_args=(
            --with-samples=1
            --with-max-tokens=8192
            --with-temperature=0
            --with-top-p=0.01
            "$@"
          )
          config_key="$(
            printf '%s\0' "''${configure_args[@]}" \
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

      runAgentEvaluation = pkgs.writeShellApplication {
        name = "verilog-agent-eval";
        runtimeInputs = with pkgs; [
          setupAgentTools
          python311
          bubblewrap
          docker_29
          nix
          iverilog
          coreutils
          bash
        ];
        text = ''
          root="''${VERILOG_EVAL_ROOT:-}"
          if [[ -z "$root" ]]; then
            root="$(git rev-parse --show-toplevel)"
          fi
          export VERILOG_EVAL_ROOT="$root"
          export VERILOG_EVAL_CACHE_ROOT="''${VERILOG_EVAL_CACHE_ROOT:-$root/.cache}"
          mkdir -p "$VERILOG_EVAL_CACHE_ROOT"
          export XDG_CACHE_HOME="$VERILOG_EVAL_CACHE_ROOT"
          export npm_config_cache="$VERILOG_EVAL_CACHE_ROOT/npm"

          verilog-agent-tools-setup

          export AGENT_EVAL_AGENT_TOOLS="$root/.agent-tools"
          export AGENT_EVAL_BWRAP=${pkgs.bubblewrap}/bin/bwrap
          export AGENT_EVAL_DOCKER=${pkgs.docker_29}/bin/docker
          export AGENT_EVAL_DOCKER_IMAGE="${agentSandboxImageName}:${agentSandboxImageTag}"
          export AGENT_EVAL_DOCKER_IMAGE_ARCHIVE=${agentSandboxImage}
          export AGENT_EVAL_TRUE=${pkgs.coreutils}/bin/true
          export AGENT_EVAL_BASH=${pkgs.bash}/bin/bash
          export AGENT_EVAL_ENV=${pkgs.coreutils}/bin/env
          export AGENT_EVAL_SANDBOX_PATH="/agent-tools/node_modules/.bin:${agentSandboxPath}"
          export AGENT_EVAL_STORE_ROOTS="${agentStoreRoots}"
          export LD_LIBRARY_PATH="${runtimeLibraryPath}:''${LD_LIBRARY_PATH:-}"
          export PYTHONPATH=${./.}

          exec python3 ${./agent_eval/runner.py} --repo-root "$root" "$@"
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
