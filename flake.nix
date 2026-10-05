{
  description = "Track what your household borrows from German public libraries";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    ha-stadtbibliothek.url = "github:makefu/ha_stadtbibliothek/v1.4.1";
    ha-stadtbibliothek.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      ha-stadtbibliothek,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [
            ha-stadtbibliothek.overlays.default
            self.overlays.default
          ];
        };

      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (pkgsFor system));
    in
    {
      overlays.default = final: _prev: {
        bib-tracker = final.callPackage ./nix/package.nix { };
      };

      packages = forAllSystems (pkgs: {
        default = pkgs.bib-tracker;
        bib-tracker = pkgs.bib-tracker;
      });

      nixosModules.default = import ./nix/module.nix;

      apps = forAllSystems (pkgs: {
        # Live checks against the real libraries and metadata services. Run
        # from the source tree, since they are about this working copy rather
        # than about a built package. Credentials come from a git-ignored
        # .secrets.yml.
        integration = {
          type = "app";
          program = "${pkgs.writeShellScript "bib-tracker-integration" ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${
              pkgs.python313.withPackages (ps: [
                ps.pytest
                ps.pytest-asyncio
                ps.respx
                ps.freezegun
                ps.asgi-lifespan
                ps.fastapi
                ps.uvicorn
                ps.jinja2
                ps.httpx
                ps.apscheduler
                ps.pydantic
                ps.pydantic-settings
                ps.python-multipart
                ps.pillow
                ps.structlog
                ps.ha-stadtbibliothek
              ])
            }/bin/pytest -m live -v tests/integration "$@"
          ''}";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python313.withPackages (ps: [
              ps.fastapi
              ps.uvicorn
              ps.jinja2
              ps.httpx
              ps.apscheduler
              ps.pydantic
              ps.pydantic-settings
              ps.pyyaml
              ps.types-pyyaml
              ps.python-multipart
              ps.pillow
              ps.structlog
              ps.ha-stadtbibliothek

              ps.pytest
              ps.pytest-asyncio
              ps.respx
              ps.freezegun
              ps.asgi-lifespan
              ps.mypy
            ]))
            pkgs.ruff
            pkgs.uv
            pkgs.sqlite
          ];
          shellHook = ''
            export PYTHONPATH=$PWD/src''${PYTHONPATH:+:$PYTHONPATH}
          '';
        };
      });

      checks = forAllSystems (
        pkgs:
        let
          typeEnv = pkgs.python313.withPackages (ps: [
            ps.mypy
            ps.fastapi
            ps.uvicorn
            ps.jinja2
            ps.httpx
            ps.apscheduler
            ps.pydantic
            ps.pydantic-settings
            ps.pyyaml
            ps.types-pyyaml
            ps.pillow
            ps.structlog
            ps.ha-stadtbibliothek
            ps.pytest
          ]);
        in
        {
          # Runs the unit suite via pytestCheckHook: the cheap gate.
          package = self.packages.${pkgs.system}.bib-tracker;

          ruff =
            pkgs.runCommand "ruff-check"
              {
                nativeBuildInputs = [ pkgs.ruff ];
                RUFF_CACHE_DIR = "/tmp/ruff-cache";
              }
              ''
                cd ${self}
                ruff check .
                ruff format --check .
                touch $out
              '';

          mypy =
            pkgs.runCommand "mypy-check"
              {
                nativeBuildInputs = [ typeEnv ];
                MYPY_CACHE_DIR = "/tmp/mypy-cache";
              }
              ''
                cd ${self}
                mypy
                touch $out
              '';
        }
        // nixpkgs.lib.optionalAttrs (pkgs.stdenv.hostPlatform.system == "x86_64-linux") {
          vm-test = import ./nix/vm-test.nix { inherit pkgs self; };
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
