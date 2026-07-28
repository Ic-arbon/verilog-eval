{
  description = "VerilogEval test environment (x86_64-linux)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      pythonRequirements = pkgs.writeText "verilog-eval-requirements.txt" ''
        langchain==0.2.17
        langchain-openai==0.1.25
        langchain-nvidia-ai-endpoints==0.2.2
        pandas==2.2.3
      '';

      setupPython = pkgs.writeShellApplication {
        name = "verilog-eval-setup";
        runtimeInputs = [ pkgs.gitMinimal pkgs.uv ];
        text = ''
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
            "import langchain, langchain_openai, langchain_nvidia_ai_endpoints, pandas"
          echo "Python dependencies are ready in $venv"
        '';
      };
    in
    {
      packages.${system}.setup = setupPython;

      apps.${system}.setup = {
        type = "app";
        program = "${setupPython}/bin/verilog-eval-setup";
        meta.description = "Install VerilogEval Python dependencies into .venv";
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
