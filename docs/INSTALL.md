# Installation

SE1 is experimental and intended for people comfortable debugging Proton.
Keep another compatibility tool available. Nothing here promises a specific
VR runtime or headset setup will work.

## Manual Steam installation

Exit Steam first. Download the SE1 archive and `SHA256SUMS` from the SE1
prerelease; run `sha256sum --check --ignore-missing SHA256SUMS` in the download
directory. It must report the SE1 binary archive as `OK`; the checksum file
also lists optional source assets. Extract into `compatibilitytools.d` without
overwriting an existing tool. On a typical Linux installation:

```sh
mkdir -p "$HOME/.local/share/Steam/compatibilitytools.d"
tar -xzf proton-ge-11-6-rtsp-se1.tar.gz \
  -C "$HOME/.local/share/Steam/compatibilitytools.d"
```

Start Steam yourself. In the game's Properties → Compatibility menu, select
`proton-ge-11-6-rtsp-se1`. If you use Flatpak Steam, use its own compatibility
tool directory instead. Do not copy an entire game prefix between tools just
to install this one.

SE1 does not require or interpret `PROTON_XR_MODE`. For a WineWayland setup,
`PROTON_USE_WAYLAND=1` remains the ordinary backend selection. Do not add
unrelated environment switches to make this candidate work. Host Discord/VR
wrappers, if needed by your setup, remain your own configuration.

## NixOS prebuilt package

Add this repository as a flake input and import its module:

```nix
inputs.rtsp-se1.url = "github:Go08er/proton-ge-real-time-streaming-protocol-slop-edition/se1-nix1";

# In the NixOS configuration, with rtsp-se1 passed through specialArgs:
imports = [ rtsp-se1.nixosModules.default ];
programs.proton-ge-rtsp.enable = true;
```

Or use the package directly:

```nix
programs.steam.extraCompatPackages = [
  rtsp-se1.packages.x86_64-linux.prebuilt
];
```

Use `extraCompatPackages`, not `environment.systemPackages`. The package
has the `steamcompattool` output expected by the NixOS Steam module, preserves
the original binaries and launcher, and verifies the release archive hash.
Only x86_64 Linux is currently supported. Configure Steam licensing and your
graphics/VR environment as you normally would; this module does not manage
your headset, network, Discord installation or game launch options.

## Your own source build

Run the [automated source-build workflow](BUILDING.md). The resulting archive
can be installed manually as above, or wrapped in the same Nix package:

```nix
programs.proton-ge-rtsp.package = pkgs.callPackage
  "${rtsp-se1}/nix/prebuilt.nix" {
    localArchive = /path/to/your/verified/proton-ge-11-6-rtsp-se1.tar.gz;
  };
```

The source-build app runs the pinned GE build in an offline Podman container;
it is deliberately not advertised as a pure `nix build` compilation derivation.
Do not disable the Nix sandbox to pretend otherwise.

Use the `se1-nix1` packaging tag, not the original `se1` tag, for Nix.
It fixes a `callPackage` argument collision in the default remote-download
path. The SE1 binary, source pin and release checksums are unchanged.

The package layout follows the
[Nixpkgs GE package](https://github.com/NixOS/nixpkgs/blob/nixos-unstable/pkgs/by-name/pr/proton-ge-bin/package.nix)
and the `extraCompatPackages` interface in the
[NixOS Steam module](https://github.com/NixOS/nixpkgs/blob/nixos-unstable/nixos/modules/programs/steam.nix).
