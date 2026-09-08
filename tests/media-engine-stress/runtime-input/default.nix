# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsStorePath,
  runtimeFamily,
  runtimeStorePath,
}:

assert builtins.typeOf nixpkgsStorePath == "string";
assert builtins.typeOf runtimeFamily == "string";
assert builtins.typeOf runtimeStorePath == "string";
assert builtins.elem runtimeFamily [
  "sniper"
  "steamrt4"
];
assert
  builtins.match "/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+" nixpkgsStorePath
  != null;
assert
  builtins.match "/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+" runtimeStorePath
  != null;

let
  nixpkgsSrc = builtins.storePath nixpkgsStorePath;
  runtime = builtins.storePath runtimeStorePath;
  pkgs = import nixpkgsSrc { };
in
pkgs.runCommandLocal "immutable-steam-runtime-${runtimeFamily}"
  {
    nativeBuildInputs = [
      pkgs.coreutils
      pkgs.findutils
      pkgs.python3
    ];
    preferLocalBuild = true;
    allowSubstitutes = false;
  }
  ''
    export LC_ALL=C.UTF-8
    export SOURCE_DATE_EPOCH=1
    mkdir -p "$out"
    find ${runtime} -mindepth 1 -maxdepth 1 ! -name var \
      -exec cp --archive --reflink=auto --target-directory="$out" -- {} +
    python3 -I ${./audit_runtime.py} \
      --source ${runtime} \
      --runtime-family ${pkgs.lib.escapeShellArg runtimeFamily} \
      --output "$out"
  ''
