{
  description = "VerilogEval test environment (x86_64-linux)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      runtimeLibraryPath = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];

      pythonRequirements = pkgs.writeText "verilog-eval-requirements.txt" ''
        langchain==0.2.17
        langchain-community==0.2.19
        langchain-openai==0.1.25
        langchain-nvidia-ai-endpoints==0.2.2
        pandas==2.2.3
      '';

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
