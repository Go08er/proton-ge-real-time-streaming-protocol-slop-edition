# SPDX-License-Identifier: BSD-3-Clause
{ nixpkgsPath
, system ? builtins.currentSystem
}:

assert builtins.isPath nixpkgsPath;
assert builtins.pathExists nixpkgsPath;
assert builtins.match "/nix/store/[^/]+" (builtins.toString nixpkgsPath) != null;

let
  pin = builtins.fromJSON (builtins.readFile ../fixtures/nixpkgs-pin.json);
  suppliedNixpkgsRoot = builtins.toPath nixpkgsPath;
  nixpkgsRoot = builtins.path {
    path = suppliedNixpkgsRoot;
    name = "media-engine-stress-pinned-nixpkgs";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot { inherit system; };
  source = builtins.path {
    name = "media-engine-stress-driver-source";
    path = ./.;
    filter = path: type:
      let name = builtins.baseNameOf path;
      in name != "build" && name != "__pycache__";
  };
in
pkgs.runCommand "media-engine-stress-driver"
  {
    nativeBuildInputs = [
      pkgs.coreutils
      pkgs.file
      pkgs.python3
      pkgs.bash
      pkgs.pkgsCross.mingwW64.stdenv.cc
    ];
  }
  ''
    export HOME="$TMPDIR/home"
    export SOURCE_DATE_EPOCH=0
    mkdir -p "$HOME" "$out/bin" "$out/evidence" "$out/share"

    cd ${source}
    MINGW_CC_X64=x86_64-w64-mingw32-gcc \
      bash ./build.sh --arch x86_64 --out "$TMPDIR/compiled" --check

    cp "$TMPDIR/compiled/media-engine-stress-x86_64.exe" "$out/bin/"
    cp README.md COPYING.LIB parse_results.py "$out/share/"
    cp ${../fixtures/nixpkgs-pin.json} "$out/evidence/nixpkgs-pin.json"
    cp -r examples "$out/share/"

    sha256sum \
      media_engine_stress.c \
      COPYING.LIB \
      README.md \
      default.nix \
      build.sh \
      parse_results.py \
      test_parse_results.py \
      test_driver_source.py \
      examples/live-av.scenario \
      examples/live-av.oracle.json \
      examples/join-at-offset.scenario \
      examples/join-at-offset.oracle.json \
      examples/seek-storm-latest-wins.scenario \
      examples/seek-storm-latest-wins.oracle.json \
      examples/vod-seek.scenario \
      examples/vod-seek.oracle.json \
      > "$out/evidence/SOURCE-SHA256SUMS"

    cd "$out"
    sha256sum bin/media-engine-stress-x86_64.exe > SHA256SUMS
    {
      printf 'nixpkgs_path=%s\n' ${nixpkgsRoot}
      printf 'nixpkgs_revision=%s\n' ${pin.revision}
      printf 'nixpkgs_nar_hash=%s\n' ${pin.nar_hash_sri}
      printf 'source_store_path=%s\n' ${source}
      x86_64-w64-mingw32-gcc --version | head -n 1
      file bin/media-engine-stress-x86_64.exe
    } > evidence/BUILD-INFO.txt
  ''
