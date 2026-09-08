{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = with pkgs; [
    bash
    binutils
    autoconf
    automake
    coreutils
    curl
    diffutils
    file
    findutils
    git
    gawk
    gnumake
    gnupatch
    gnugrep
    gnused
    gnutar
    gzip
    jq
    libtool
    mediamtx
    patchelf
    perl
    pkg-config
    podman
    python3
    ripgrep
    rsync
    shellcheck
    tree
    unzip
    util-linux
    vulkan-tools
    which
    zstd
    ffmpeg
    pkgsCross.mingwW64.stdenv.cc
  ];
}
