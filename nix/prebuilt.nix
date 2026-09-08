{ lib, stdenvNoCC, fetchurl, src ? null }:
let
  release = import ./release.nix;
in stdenvNoCC.mkDerivation {
  pname = "proton-ge-rtsp-slop-edition";
  version = "se1";
  src = if src != null then src else fetchurl {
    inherit (release) url hash;
  };
  sourceRoot = "proton-ge-11-6-rtsp-se1";
  outputs = [ "out" "steamcompattool" ];
  dontConfigure = true;
  dontBuild = true;
  dontFixup = true;
  dontStrip = true;
  dontPatchShebangs = true;
  installPhase = ''
    runHook preInstall
    echo "Use programs.steam.extraCompatPackages, not environment.systemPackages." > "$out"
    mkdir -p "$steamcompattool"
    cp -a ./. "$steamcompattool/"
    runHook postInstall
  '';
  meta = {
    description = "Experimental GE-Proton11-6 RTSP compatibility tool";
    homepage = "https://github.com/Go08er/proton-ge-real-time-streaming-protocol-slop-edition";
    platforms = [ "x86_64-linux" ];
    # Aggregate: component/file licenses remain authoritative.
    license = with lib.licenses; [ bsd3 lgpl21Plus gpl2Plus gpl3Plus mit zlib ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
