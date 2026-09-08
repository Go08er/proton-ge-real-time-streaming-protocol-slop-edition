# SPDX-License-Identifier: BSD-3-Clause
{ fetchurl, lib, stdenvNoCC, gnutar, gzip }:

stdenvNoCC.mkDerivation {
  pname = "mediamtx-rtsp-test";
  version = "1.19.2";

  src = fetchurl {
    url = "https://github.com/bluenviron/mediamtx/releases/download/v1.19.2/mediamtx_v1.19.2_linux_amd64.tar.gz";
    hash = "sha256-+cYBzDA87Kj60og5F7AiiCZyxbxWMR6S286xbl8gxgw=";
  };

  nativeBuildInputs = [ gnutar gzip ];
  dontUnpack = true;
  strictDeps = true;

  installPhase = ''
    runHook preInstall
    work=$(mktemp -d)
    tar -xzf "$src" -C "$work"
    test "$(find "$work" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')" = \
      'LICENSE mediamtx mediamtx.yml '
    echo '7dd8b247dac4105fa938fbae6222ebfe6e7889cdcfba219d65ea88144153c3c0  mediamtx' |
      (cd "$work" && sha256sum -c -)
    echo 'ecae73b0a23185a35a1222edc0aae5245bb9739420bad0125b0c42e935301a80  LICENSE' |
      (cd "$work" && sha256sum -c -)
    install -Dm0555 "$work/mediamtx" "$out/bin/mediamtx"
    install -Dm0444 "$work/LICENSE" "$out/share/licenses/mediamtx/LICENSE"
    runHook postInstall
  '';

  meta = {
    description = "Pinned MediaMTX binary used only by the loopback RTSP qualification fixture";
    homepage = "https://github.com/bluenviron/mediamtx";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    sourceProvenance = with lib.sourceTypes; [ binaryNativeCode ];
  };
}
