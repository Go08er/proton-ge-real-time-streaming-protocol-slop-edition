# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsStorePath,
  archiveStorePath,
  archiveSha256,
  topLevelIdentity,
  displayName ? topLevelIdentity,
}:

assert builtins.typeOf nixpkgsStorePath == "string";
assert builtins.typeOf archiveStorePath == "string";
assert
  builtins.match "/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+" nixpkgsStorePath
  != null;
assert
  builtins.match "/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+" archiveStorePath
  != null;
assert builtins.match "[0-9a-f]{64}" archiveSha256 != null;
assert builtins.match "[A-Za-z0-9][A-Za-z0-9._+-]{0,127}" topLevelIdentity != null;
assert builtins.typeOf displayName == "string";
assert builtins.stringLength displayName > 0;
assert builtins.stringLength displayName <= 256;

let
  # Path-valued arguments cause Nix to import an existing flat store file under
  # a second store name.  A strictly validated string plus storePath retains
  # the exact caller-pinned store identity and its dependency context.
  nixpkgsSrc = builtins.storePath nixpkgsStorePath;
  archive = builtins.storePath archiveStorePath;
  pkgs = import nixpkgsSrc { };
in
pkgs.runCommandLocal "immutable-${topLevelIdentity}"
  {
    nativeBuildInputs = [ pkgs.python3 ];
    preferLocalBuild = true;
    allowSubstitutes = false;
  }
  ''
    export LC_ALL=C.UTF-8
    export SOURCE_DATE_EPOCH=1
    python3 -I ${./validate_and_extract.py} \
      --archive ${archive} \
      --archive-sha256 ${archiveSha256} \
      --top-level-identity ${pkgs.lib.escapeShellArg topLevelIdentity} \
      --display-name ${pkgs.lib.escapeShellArg displayName} \
      --nixpkgs-source ${nixpkgsSrc} \
      --output "$out"
  ''
