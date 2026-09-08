{
  description = "Experimental GE-Proton11-6 RTSP Slop Edition";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/dc5d91f840324650bac8c379428c7037a416959a";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      buildTools = with pkgs; [
        bashInteractive coreutils git gnumake autoconf automake libtool m4
        perl python3 podman gnupatch gnutar gzip xz zstd unzip rsync ripgrep
        curl file binutils gcc ffmpeg pkg-config patchelf util-linux findutils gawk
        gnused gnugrep diffutils nix
      ];
      builder = pkgs.writeShellApplication {
        name = "build-proton-ge-rtsp-se1";
        runtimeInputs = buildTools;
        text = ''
          export SE1_PROJECT_KIT=${self}
          exec bash ${./scripts/build-se1.sh} "$@"
        '';
      };
    in {
      packages.${system} = {
        default = self.packages.${system}.prebuilt;
        prebuilt = pkgs.callPackage ./nix/prebuilt.nix { };
        build-from-source = builder;
      };
      apps.${system}.build-from-source = {
        type = "app";
        program = "${builder}/bin/build-proton-ge-rtsp-se1";
      };
      devShells.${system}.default = pkgs.mkShell { packages = buildTools; };
      nixosModules.default = { config, lib, pkgs, ... }:
        let cfg = config.programs.proton-ge-rtsp;
        in {
          options.programs.proton-ge-rtsp = {
            enable = lib.mkEnableOption "the experimental SE1 Steam compatibility tool";
            package = lib.mkOption {
              type = lib.types.package;
              default = pkgs.callPackage ./nix/prebuilt.nix { };
              description = "Prebuilt SE1, or a package wrapping your verified local build.";
            };
          };
          config = lib.mkIf cfg.enable {
            assertions = [{
              assertion = pkgs.stdenv.hostPlatform.system == system;
              message = "SE1 currently supports x86_64-linux only.";
            }];
            programs.steam.enable = true;
            programs.steam.extraCompatPackages = [ cfg.package ];
          };
        };
    };
}
