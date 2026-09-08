# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  driverExePath,
  fixtureRootPath,
  system ? builtins.currentSystem,
  variant ? "running-replace",
  sourceScheme ? "rtsp",
  payloadRung ? null,
  reproductionInput ? null,
  mediamtxBinPath ? null,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;
assert builtins.isString driverExePath;
assert builtins.match "^/nix/store/[^/]+-.*" driverExePath != null;
assert builtins.isString fixtureRootPath;
assert builtins.match "^/nix/store/[^/]+-.*" fixtureRootPath != null;
assert builtins.elem variant [
  "running-replace"
  "pause-before-replace"
  "blackhole-cancel"
  "finite-hold-recovery"
  "drain-diagnostics"
];
assert builtins.elem sourceScheme [ "rtsp" "rtspt" ];
assert payloadRung == null || builtins.elem payloadRung [ "a" "b" "c" "d" ];
assert reproductionInput == null || builtins.elem reproductionInput [
  "observed-heavy-v1"
  "observed-midgop-v1"
];
assert payloadRung == null || reproductionInput == null;
assert variant != "finite-hold-recovery" || reproductionInput == "observed-heavy-v1";
assert variant != "drain-diagnostics"
  || (sourceScheme == "rtspt" && reproductionInput == "observed-heavy-v1");
assert mediamtxBinPath == null || builtins.isString mediamtxBinPath;
assert mediamtxBinPath == null
  || builtins.match "^/nix/store/[0-9a-z]{32}-.*" mediamtxBinPath != null;

let
  pin = builtins.fromJSON (builtins.readFile ../fixtures/nixpkgs-pin.json);
  nixpkgsRoot = builtins.path {
    path = builtins.toPath nixpkgsPath;
    name = "source";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot { inherit system; config.allowUnfree = false; };
  driverExe = builtins.storePath driverExePath;
  fixtureRoot = builtins.storePath fixtureRootPath;
  mediamtxInput =
    if mediamtxBinPath == null
    then null
    else builtins.storePath mediamtxBinPath;
  mediamtx =
    if mediamtxBinPath == null
    then pkgs.callPackage ./mediamtx-1.19.2.nix { }
    else pkgs.runCommandLocal "mediamtx-rtsp-test-1.19.2-local"
      { nativeBuildInputs = [ pkgs.coreutils ]; }
      ''
        echo '7dd8b247dac4105fa938fbae6222ebfe6e7889cdcfba219d65ea88144153c3c0  ${mediamtxInput}' |
          sha256sum -c -
        test -x ${pkgs.lib.escapeShellArg (toString mediamtxInput)}
        install -Dm0555 ${pkgs.lib.escapeShellArg (toString mediamtxInput)} \
          "$out/bin/mediamtx"
      '';
  mediamtxBin = "${mediamtx}/bin/mediamtx";
  ffmpegBin = pkgs.lib.getBin pkgs.ffmpeg;
  adapter = ../../host-runtime/rtsp_live_case.py;
  resultContract = ../../nix-vm/result_contract.py;
  scenarios = {
    running-replace = ./examples/live-tcp-reopen.scenario;
    pause-before-replace = ./examples/live-tcp-pause-reopen.scenario;
    blackhole-cancel = ./examples/live-tcp-blackhole-cancel.scenario;
    finite-hold-recovery = ./examples/live-tcp-finite-hold-recovery.scenario;
    drain-diagnostics = ./examples/live-tcp-drain-diagnostics.scenario;
  };
  caseIds = {
    running-replace = "rtsp-live-tcp-reopen";
    pause-before-replace = "rtsp-live-tcp-pause-before-replace";
    blackhole-cancel = "rtsp-live-tcp-blackhole-cancel";
    finite-hold-recovery = "rtsp-live-tcp-finite-hold-recovery";
    drain-diagnostics = "rtsp-live-tcp-drain-diagnostics";
  };
  relayModes = {
    running-replace = "forward";
    pause-before-replace = "forward";
    blackhole-cancel = "server-to-client-blackhole";
    finite-hold-recovery = "server-to-client-finite-hold";
    drain-diagnostics = "forward";
  };
  blackholeThresholds = {
    running-replace = 0;
    pause-before-replace = 0;
    blackhole-cancel = 2097152;
    finite-hold-recovery = 4194304;
    drain-diagnostics = 0;
  };
  holdDurations = {
    running-replace = 0;
    pause-before-replace = 0;
    blackhole-cancel = 0;
    finite-hold-recovery = 3000;
    drain-diagnostics = 0;
  };
  minimumConnections = {
    running-replace = 3;
    pause-before-replace = 3;
    blackhole-cancel = 2;
    finite-hold-recovery = 2;
    drain-diagnostics = 10;
  };
  controlOracles = {
    running-replace = ./examples/live-tcp-reopen.control.oracle.json.in;
    pause-before-replace = ./examples/live-tcp-reopen.control.oracle.json.in;
    blackhole-cancel = ./examples/live-tcp-blackhole-cancel.control.oracle.json.in;
    finite-hold-recovery = ./examples/live-tcp-finite-hold-recovery.control.oracle.json.in;
    drain-diagnostics = ./examples/live-tcp-drain-diagnostics.control.oracle.json.in;
  };
  instrumentedOracles = {
    running-replace = ./examples/live-tcp-reopen.instrumented.oracle.json.in;
    pause-before-replace = ./examples/live-tcp-reopen.instrumented.oracle.json.in;
    blackhole-cancel = ./examples/live-tcp-blackhole-cancel.instrumented.oracle.json.in;
    finite-hold-recovery = ./examples/live-tcp-finite-hold-recovery.instrumented.oracle.json.in;
    drain-diagnostics = ./examples/live-tcp-drain-diagnostics.instrumented.oracle.json.in;
  };
  selectedScenario = builtins.getAttr variant scenarios;
  selectedCaseId = builtins.getAttr variant caseIds;
  selectedRelayMode = builtins.getAttr variant relayModes;
  selectedBlackholeThreshold = builtins.getAttr variant blackholeThresholds;
  selectedHoldDuration = builtins.getAttr variant holdDurations;
  selectedMinimumConnections = builtins.getAttr variant minimumConnections;
  selectedControlOracle = builtins.getAttr variant controlOracles;
  selectedInstrumentedOracle = builtins.getAttr variant instrumentedOracles;
  payloadPaths = {
    a = "rtsp/payload-a-320x180-main-270k.mp4";
    b = "rtsp/payload-b-1280x720-main-270k.mp4";
    c = "rtsp/payload-c-1280x720-high-270k.mp4";
    d = "rtsp/payload-d-1280x720-high-8200k.mp4";
  };
  selectedFixtureRelativePath =
    if reproductionInput == "observed-heavy-v1" then "rtsp/repro-observed-heavy-v1.mp4"
    else if reproductionInput == "observed-midgop-v1" then "rtsp/repro-observed-midgop-v1.mp4"
    else if payloadRung == null then "av/faststart.mp4"
    else builtins.getAttr payloadRung payloadPaths;
  caseSuffix =
    (if sourceScheme == "rtsp" then "" else "-rtspt")
    + (if payloadRung == null then "" else "-rung-${payloadRung}")
    + (if reproductionInput == null then "" else "-repro-${reproductionInput}");
in

assert pkgs.lib.assertMsg (system == "x86_64-linux")
  "the pinned MediaMTX qualification binary is x86_64-linux only";
assert pkgs.lib.assertMsg (builtins.pathExists driverExe) "driverExePath does not exist";
assert pkgs.lib.assertMsg (builtins.pathExists fixtureRoot) "fixtureRootPath does not exist";
assert pkgs.lib.assertMsg
  (mediamtxInput == null || builtins.pathExists mediamtxInput)
  "mediamtxBinPath does not exist";
assert pkgs.lib.assertMsg (builtins.pathExists (fixtureRoot + "/provenance/manifest.json"))
  "fixtureRootPath has no provenance manifest";

pkgs.runCommand "rtsp-media-stress-live-tcp-${variant}${caseSuffix}-case" {
  nativeBuildInputs = [ pkgs.coreutils pkgs.python3 ];
  strictDeps = true;
} ''
  echo '7dd8b247dac4105fa938fbae6222ebfe6e7889cdcfba219d65ea88144153c3c0  ${mediamtxBin}' |
    sha256sum -c -
  test -x ${pkgs.lib.escapeShellArg (toString mediamtxBin)}
  install -d "$out"
  install -m 0444 ${./mediamtx.yml} "$out/mediamtx.yml"
  substitute ${selectedScenario} "$out/scenario" \
    --replace-fail 'rtsp://' '${sourceScheme}://'
  substitute ${./examples/service-config.json.in} "$out/service-config.json" \
    --replace-fail '@CASE_ID@' '${selectedCaseId}' \
    --replace-fail '@RELAY_MODE@' '${selectedRelayMode}' \
    --replace-fail '"@BLACKHOLE_AFTER_SERVER_BYTES@"' '${toString selectedBlackholeThreshold}' \
    --replace-fail '"@HOLD_DURATION_MS@"' '${toString selectedHoldDuration}' \
    --replace-fail '"@MIN_OBSERVED_CONNECTIONS@"' '${toString selectedMinimumConnections}' \
    --replace-fail '@MEDIAMTX_BIN@' '${mediamtxBin}' \
    --replace-fail '@FFMPEG_BIN@' '${ffmpegBin}/bin/ffmpeg' \
    --replace-fail '@FFPROBE_BIN@' '${ffmpegBin}/bin/ffprobe' \
    --replace-fail '@SOURCE_SCHEME@' '${sourceScheme}' \
    --replace-fail '@FIXTURE_RELATIVE_PATH@' '${selectedFixtureRelativePath}' \
    --replace-fail '@FIXTURE_SHA256@' \
      "$(sha256sum ${pkgs.lib.escapeShellArg (toString fixtureRoot + "/${selectedFixtureRelativePath}")} | cut -d ' ' -f 1)"

  driver_sha256=$(sha256sum ${pkgs.lib.escapeShellArg (toString driverExe)} | cut -d ' ' -f 1)
  scenario_sha256=$(sha256sum "$out/scenario" | cut -d ' ' -f 1)
  substitute ${selectedControlOracle} "$out/control.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"
  substitute ${selectedInstrumentedOracle} "$out/instrumented.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"

  ${pkgs.python3}/bin/python3 ${resultContract} check-stress-oracles \
    --control "$out/control.oracle.json" \
    --instrumented "$out/instrumented.oracle.json" \
    --driver-sha256 "$driver_sha256" \
    --scenario-sha256 "$scenario_sha256" \
    --media-kind av
  ${pkgs.python3}/bin/python3 ${adapter} check \
    --config "$out/service-config.json" \
    --fixture-root ${pkgs.lib.escapeShellArg (toString fixtureRoot)} \
    --fixture-manifest ${pkgs.lib.escapeShellArg (toString fixtureRoot + "/provenance/manifest.json")} \
    --scenario "$out/scenario" >/dev/null

  cd "$out"
  sha256sum scenario service-config.json mediamtx.yml \
    control.oracle.json instrumented.oracle.json > SHA256SUMS
''
