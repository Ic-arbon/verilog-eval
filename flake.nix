{
  description = "VerilogEval test environment (x86_64-linux)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          # Verilog compilation and simulation
          iverilog
          verilator

          # Evaluation harness
          python311
          uv
          gnumake
          bash
          coreutils  # seq, timeout, expr
          util-linux # column
        ];

        # Makefile.in uses Bash-specific syntax such as [[ ... ]] and PIPESTATUS.
        MAKEFLAGS = "SHELL=${pkgs.bash}/bin/bash";

        shellHook = ''
          echo "VerilogEval test environment ready"
          echo "  iverilog : $(iverilog -V 2>&1 | head -n1)"
          echo "  verilator: $(verilator --version)"
          echo "  python   : $(python3 --version)"
        '';
      };
    };
}
